"""Incoming webhooks: Anthropic (verified by the SDK), GitHub (merged PRs), Linear and Jira (new or changed
requests)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Connector, ConnectorStatus, User, UserSettings
from app.security import decrypt

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/anthropic")
async def anthropic_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    secret = get_settings().webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    body = (await request.body()).decode()
    # Verification is local (HMAC): no API key is needed.
    verifier = anthropic.Anthropic(api_key="webhook-verification-only")
    try:
        event = verifier.beta.webhooks.unwrap(body, headers=dict(request.headers), key=secret)
    except Exception:  # invalid signature, timestamp too old, unreadable payload
        raise HTTPException(status_code=400, detail="Invalid signature.") from None
    data = event.data
    if data.type == "vault_credential.refresh_failed":
        connector = db.scalar(select(Connector).where(Connector.credential_id == data.id))
        if connector:
            connector.status = ConnectorStatus.ERROR
            connector.status_message = "Reconnect needed: the OAuth token refresh failed."
            db.commit()
            log.info("Connector %s flagged as error (refresh_failed)", connector.id)
    return Response(status_code=204)


def github_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub signs the raw body with HMAC-SHA256 in X-Hub-Signature-256: "sha256=<hex>"."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/webhooks/github/{hook_id}")
async def github_webhook(hook_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    settings = db.scalar(select(UserSettings).where(UserSettings.github_hook_id == hook_id)) if hook_id else None
    if settings is None or not settings.github_hook_secret_encrypted:
        raise HTTPException(status_code=404)
    body = await request.body()
    if not github_signature_ok(decrypt(settings.github_hook_secret_encrypted), body,
                               request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return JSONResponse({"ok": True, "message": "Maestro is listening."})
    if event != "pull_request":
        return JSONResponse({"ignored": event})
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.") from None
    pr = payload.get("pull_request") or {}
    if payload.get("action") != "closed" or not pr.get("merged"):
        return JSONResponse({"ignored": f"pull_request.{payload.get('action')}"})
    repo = (payload.get("repository") or {}).get("full_name") or ""
    user = db.get(User, settings.user_id)
    if not repo or user is None:
        return JSONResponse({"ignored": "no repository"})
    from app.maestro.service import wake_for_repo

    woken = await run_in_threadpool(wake_for_repo, db, user, repo)
    log.info("GitHub: PR #%s merged on %s, %d workflow(s) woken", pr.get("number"), repo, len(woken))
    return JSONResponse({"repository": repo, "woken_workflows": woken}, status_code=202)


# --- Linear: wake the projects of a team when a request is created or changes --------------

LINEAR_MAX_AGE_MS = 120_000  # Linear advises checking webhookTimestamp; allow a little clock drift


def linear_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """Linear signs the raw body with HMAC-SHA256, hex-encoded, in the Linear-Signature header."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _project_hints(obj: Any) -> set[str]:
    """How the Linear project of an issue appears in a payload: nested project (id, name, url, slugId), projectId."""
    hints: set[str] = set()
    if not isinstance(obj, dict):
        return hints
    project = obj.get("project")
    if isinstance(project, dict):
        hints.update(str(project.get(k) or "") for k in ("id", "name", "url", "slugId"))
    hints.add(str(obj.get("projectId") or ""))
    return {h for h in hints if h}


def _known_aliases(db: Session, user_id: int, hints: set[str]) -> set[str]:
    """Add the other names Maestro has seen for the same Linear project (an event may carry only its id,
    another its id, name and URL), so a setting holding the name or URL still matches."""
    from app.models import LinearIssueProject

    if not hints:
        return hints
    known = set(hints)
    for row in db.scalars(select(LinearIssueProject).where(LinearIssueProject.user_id == user_id)):
        if known & set(row.project_hints or []):
            known |= set(row.project_hints)
    return known


def linear_issue_project(db: Session, user_id: int, event: str, data: dict[str, Any]) -> set[str]:
    """The Linear project of the event's issue. Issue events carry it (and Maestro remembers it); comment events
    usually carry only the issue id, so the project is looked up from what Maestro learned earlier."""
    from app.models import LinearIssueProject

    if event == "Issue":
        hints = _known_aliases(db, user_id, _project_hints(data))
        issue_id = str(data.get("id") or "")
        if issue_id:
            row = db.scalar(select(LinearIssueProject).where(LinearIssueProject.user_id == user_id,
                                                             LinearIssueProject.issue_id == issue_id))
            if row is None:
                db.add(LinearIssueProject(user_id=user_id, issue_id=issue_id, project_hints=sorted(hints)))
            else:
                row.project_hints = sorted(hints)
            db.commit()
        return hints
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
    hints = _known_aliases(db, user_id, _project_hints(issue))
    if hints:
        return hints
    issue_id = str(data.get("issueId") or issue.get("id") or "")
    row = db.scalar(select(LinearIssueProject).where(LinearIssueProject.user_id == user_id,
                                                     LinearIssueProject.issue_id == issue_id)) if issue_id else None
    return set(row.project_hints) if row else set()


def linear_wake_reason(event: str, payload: dict[str, Any]) -> str | None:
    """Why this Linear event should wake the projects of its team (None: ignore it)."""
    action, data = payload.get("action"), payload.get("data") or {}
    if event == "Issue" and action == "create":
        return f"new Linear issue {data.get('identifier') or ''}".strip()
    if event == "Issue" and action == "update":
        changed = set((payload.get("updatedFrom") or {}).keys())
        if changed & {"stateId", "labelIds"}:  # a person moved it (validation, status) or labeled it
            return f"Linear issue {data.get('identifier') or ''} changed".strip()
        return None
    if event == "Comment" and action == "create":
        issue = data.get("issue") or {}
        return f"new comment on Linear issue {issue.get('identifier') or ''}".strip()
    return None


@router.post("/webhooks/linear/{hook_id}")
async def linear_webhook(hook_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    settings = db.scalar(select(UserSettings).where(UserSettings.linear_hook_id == hook_id)) if hook_id else None
    if settings is None:
        raise HTTPException(status_code=404)
    if not settings.linear_hook_secret_encrypted:
        raise HTTPException(status_code=401, detail="Paste the Linear signing secret in Maestro first.")
    body = await request.body()
    if not linear_signature_ok(decrypt(settings.linear_hook_secret_encrypted), body,
                               request.headers.get("linear-signature")):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.") from None
    sent = payload.get("webhookTimestamp")
    if not isinstance(sent, (int, float)) or abs(time.time() * 1000 - sent) > LINEAR_MAX_AGE_MS:
        raise HTTPException(status_code=400, detail="Stale or missing webhookTimestamp.")
    event = request.headers.get("linear-event") or str(payload.get("type") or "")
    why = linear_wake_reason(event, payload)
    if why is None:
        return JSONResponse({"ignored": f"{event}.{payload.get('action')}"})
    data = payload.get("data") or {}
    # Field names of Linear's payloads are only partly documented: log their keys (never their values).
    log.info("Linear %s.%s data keys: %s", event, payload.get("action"), sorted(data.keys()) if isinstance(data, dict) else "?")
    user = db.get(User, settings.user_id)
    if user is None or not isinstance(data, dict):
        return JSONResponse({"ignored": "no user"})
    projects = await run_in_threadpool(linear_issue_project, db, user.id, event, data)
    if not projects:
        return JSONResponse({"ignored": "issue not in a known Linear project"})
    from app.maestro.service import wake_for_linear

    woken = await run_in_threadpool(wake_for_linear, db, user, projects, why)
    log.info("Linear: %s, %d project(s) woken", why, len(woken))
    return JSONResponse({"woken_workflows": woken}, status_code=202)


# --- Jira: wake the projects of a Jira project when a request is created or changes -------

JIRA_MAX_AGE_MS = 600_000  # Jira retries failed deliveries: accept a signed call up to 10 minutes old
JIRA_FIELDS = {"status", "labels", "resolution"}


def _jira_hints(payload: dict[str, Any]) -> set[str]:
    """Ways a project may name the issue's Jira project: key (from "ELI-21" too), name, id."""
    issue = payload.get("issue") or {}
    hints: set[str] = set()
    project = ((issue.get("fields") or {}).get("project")) or {}
    if isinstance(project, dict):
        hints.update(str(project.get(k) or "") for k in ("key", "name", "id"))
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)-\d+$", str(issue.get("key") or ""))
    if m:
        hints.add(m.group(1))
    return {h for h in hints if h}


def jira_wake_reason(payload: dict[str, Any]) -> str | None:
    """Why this Jira event should wake the projects of its Jira project (None: ignore it)."""
    event, key = payload.get("webhookEvent"), (payload.get("issue") or {}).get("key") or ""
    if event == "jira:issue_created":
        return f"new Jira issue {key}".strip()
    if event == "jira:issue_updated":
        items = (payload.get("changelog") or {}).get("items") or []
        if any(str(i.get("field") or "").lower() in JIRA_FIELDS for i in items if isinstance(i, dict)):
            return f"Jira issue {key} changed".strip()
        return None
    if event == "comment_created":
        return f"new comment on Jira issue {key}".strip()
    return None


@router.post("/webhooks/jira/{hook_id}")
async def jira_webhook(hook_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    settings = db.scalar(select(UserSettings).where(UserSettings.jira_hook_id == hook_id)) if hook_id else None
    if settings is None or not settings.jira_hook_secret_encrypted:
        raise HTTPException(status_code=404)
    body = await request.body()
    # Same scheme as GitHub: "sha256=<hex HMAC of the raw body>", here in X-Hub-Signature.
    if not github_signature_ok(decrypt(settings.jira_hook_secret_encrypted), body,
                               request.headers.get("x-hub-signature")):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.") from None
    sent = payload.get("timestamp")
    if isinstance(sent, (int, float)) and abs(time.time() * 1000 - sent) > JIRA_MAX_AGE_MS:
        raise HTTPException(status_code=400, detail="Stale timestamp.")
    why = jira_wake_reason(payload)
    if why is None:
        return JSONResponse({"ignored": str(payload.get("webhookEvent"))})
    projects = _jira_hints(payload)
    user = db.get(User, settings.user_id)
    if not projects or user is None:
        return JSONResponse({"ignored": "no project"})
    from app.maestro.service import wake_for_jira

    woken = await run_in_threadpool(wake_for_jira, db, user, projects, why)
    log.info("Jira: %s, %d project(s) woken", why, len(woken))
    return JSONResponse({"projects": sorted(projects), "woken_workflows": woken}, status_code=202)
