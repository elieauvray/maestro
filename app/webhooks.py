"""Incoming webhooks: Anthropic (verified by the SDK), GitHub (merged PRs) and Linear (new or changed requests)."""

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


def _team_hints(data: dict[str, Any]) -> set[str]:
    """Ways a project may name the request's team: key (from "ELI-14" too), name, id."""
    hints: set[str] = set()
    for obj in (data, data.get("issue") or {}):
        if not isinstance(obj, dict):
            continue
        team = obj.get("team") or {}
        if isinstance(team, dict):
            hints.update(str(team.get(k) or "") for k in ("key", "name", "id"))
        hints.add(str(obj.get("teamId") or ""))
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*)-\d+$", str(obj.get("identifier") or ""))
        if m:
            hints.add(m.group(1))
    return {h for h in hints if h}


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
    teams = _team_hints(payload.get("data") or {})
    user = db.get(User, settings.user_id)
    if not teams or user is None:
        return JSONResponse({"ignored": "no team"})
    from app.maestro.service import wake_for_linear

    woken = await run_in_threadpool(wake_for_linear, db, user, teams, why)
    log.info("Linear: %s, %d project(s) woken", why, len(woken))
    return JSONResponse({"teams": sorted(teams), "woken_workflows": woken}, status_code=202)
