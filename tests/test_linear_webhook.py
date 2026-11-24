from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from app.db import SessionLocal
from app.models import Connector, ConnectorStatus, Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user

SECRET = "lin_wh_test_signing_secret_0123456789"


def _payload(event: str, action: str, data: dict, **extra) -> bytes:  # type: ignore[no-untyped-def]
    return json.dumps({"type": event, "action": action, "data": data,
                       "webhookTimestamp": int(time.time() * 1000), **extra}).encode()


def _post(client, path: str, event: str, body: bytes, secret: str = SECRET):  # type: ignore[no-untyped-def]
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(path, content=body, headers={"Linear-Signature": sig, "Linear-Event": event,
                                                    "Content-Type": "application/json"})


def _setup(client):  # type: ignore[no-untyped-def]
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        db.add(Connector(user_id=uid, provider="linear", mcp_server_url="https://mcp.linear.app/mcp",
                         auth_type="static_bearer", credential_id="cred_lin", last4="abcd",
                         permission_policy="always_allow", status=ConnectorStatus.CONFIGURED))
        design = {"summary": "s", "steps": [], "connectors_needed": ["linear"],
                  "config": [{"key": "LINEAR_TEAM", "description": "team", "value": "ELI"}]}
        tma = Workflow(user_id=uid, name="TMA", description="Client support", content="# Mission",
                       design_json=design, design_status="ready")
        other = Workflow(user_id=uid, name="Other", description="Works with team DELIVERY", content="# Mission",
                         design_json={**design, "config": []}, design_status="ready")
        db.add_all([tma, other])
        db.commit()
        ids = (tma.id, other.id)
    login(client, "bob")
    token = csrf(client, "/settings")
    client.post("/settings/linear-webhook", data={"csrf_token": token})
    page = client.get("/settings").text
    path = re.search(r'value="https://testserver(/webhooks/linear/[^"]+)"', page).group(1)
    assert "Waiting for the secret" in page
    return uid, path, ids, token


def test_linear_events_wake_the_projects_of_that_team(client, fake):
    uid, path, (tma, other), token = _setup(client)
    body = _payload("Issue", "create", {"identifier": "ELI-21", "team": {"key": "ELI", "name": "Elie"}})
    assert _post(client, path, "Issue", body).status_code == 401  # no signing secret yet

    client.post("/settings/linear-webhook/secret", data={"secret": SECRET, "csrf_token": token})
    assert ">On<" in client.get("/settings").text.split('id="linear-webhook"')[1][:400]
    assert _post(client, path, "Issue", body, secret="wrong-secret-0123456789").status_code == 401

    r = _post(client, path, "Issue", body)
    assert r.status_code == 202 and r.json()["woken_workflows"] == [tma]  # "ELI" is not matched inside "DELIVERY"
    with SessionLocal() as db:
        run = db.query(WorkflowRun).filter_by(workflow_id=tma).one()
        assert run.trigger == "wake" and run.status == "running"

    # While that run is going, Linear events (including Maestro's own comments) do not start another one
    comment = _payload("Comment", "create", {"body": "Ledger", "issue": {"identifier": "ELI-21"}})
    assert _post(client, path, "Comment", comment).json()["woken_workflows"] == []

    with SessionLocal() as db:  # the run ended long ago
        run = db.query(WorkflowRun).filter_by(workflow_id=tma).one()
        run.status = "completed"
        from datetime import timedelta

        from app.models import utcnow

        run.started_at = utcnow() - timedelta(minutes=10)
        db.get(Workflow, tma).last_woken_at = utcnow() - timedelta(minutes=10)
        db.commit()
    moved = _payload("Issue", "update", {"identifier": "ELI-21"}, updatedFrom={"stateId": "s1"})
    assert _post(client, path, "Issue", moved).json()["woken_workflows"] == [tma]


def test_linear_events_that_do_not_matter_are_ignored(client, fake):
    uid, path, (tma, _other), token = _setup(client)
    client.post("/settings/linear-webhook/secret", data={"secret": SECRET, "csrf_token": token})
    title_only = _payload("Issue", "update", {"identifier": "ELI-3"}, updatedFrom={"title": "old"})
    assert "ignored" in _post(client, path, "Issue", title_only).json()
    assert "ignored" in _post(client, path, "Project", _payload("Project", "create", {"name": "x"})).json()
    stale = json.dumps({"type": "Issue", "action": "create", "data": {"identifier": "ELI-4"},
                        "webhookTimestamp": int(time.time() * 1000) - 600_000}).encode()
    assert _post(client, path, "Issue", stale).status_code == 400
    with SessionLocal() as db:
        assert db.query(WorkflowRun).count() == 0

    client.post("/settings/linear-webhook/delete", data={"csrf_token": token})
    body = _payload("Issue", "create", {"identifier": "ELI-5"})
    assert _post(client, path, "Issue", body).status_code == 404
