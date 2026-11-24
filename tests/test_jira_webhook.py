from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from app.db import SessionLocal
from app.models import Connector, ConnectorStatus, Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user


def _event(event: str, key: str, **extra) -> bytes:  # type: ignore[no-untyped-def]
    return json.dumps({"webhookEvent": event, "timestamp": int(time.time() * 1000),
                       "issue": {"key": key, "fields": {"project": {"key": key.split("-")[0]}}}, **extra}).encode()


def _post(client, path: str, body: bytes, secret: str):  # type: ignore[no-untyped-def]
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(path, content=body, headers={"X-Hub-Signature": sig, "Content-Type": "application/json"})


def _setup(client):  # type: ignore[no-untyped-def]
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        db.add(Connector(user_id=uid, provider="atlassian", mcp_server_url="https://mcp.atlassian.com/v2/mcp",
                         auth_type="static_bearer", credential_id="cred_jira", last4="abcd",
                         permission_policy="always_allow", status=ConnectorStatus.CONFIGURED))
        design = {"summary": "s", "steps": [], "connectors_needed": ["atlassian"],
                  "config": [{"key": "JIRA_PROJECT", "description": "project", "value": "SUP"}]}
        support = Workflow(user_id=uid, name="Support", description="Client support", content="# Mission",
                           design_json=design, design_status="ready")
        db.add(support)
        db.commit()
        wf_id = support.id
    login(client, "bob")
    token = csrf(client, "/settings")
    client.post("/settings/jira-webhook", data={"csrf_token": token}, follow_redirects=False)
    page = client.get("/settings").text
    path = re.search(r'value="https://testserver(/webhooks/jira/[^"]+)"', page).group(1)
    secret = re.search(r'Secret \(shown once\) <input value="([^"]+)"', page.split('id="jira-webhook"')[1]).group(1)
    assert "Secret (shown once)" not in client.get("/settings").text.split('id="jira-webhook"')[1][:3000]
    return path, secret, wf_id, token


def test_jira_events_wake_the_project_and_bad_calls_are_refused(client, fake):
    path, secret, wf_id, token = _setup(client)
    created = _event("jira:issue_created", "SUP-12")
    assert _post(client, path, created, "wrong-secret").status_code == 401
    r = _post(client, path, created, secret)
    assert r.status_code == 202 and r.json()["woken_workflows"] == [wf_id]
    with SessionLocal() as db:
        assert db.query(WorkflowRun).filter_by(workflow_id=wf_id).one().trigger == "wake"
    # While the run is going, nothing else wakes it
    comment = _event("comment_created", "SUP-12", comment={"body": "Ledger"})
    assert _post(client, path, comment, secret).json()["woken_workflows"] == []
    # Other projects and irrelevant changes are ignored
    assert _post(client, path, _event("jira:issue_created", "OPS-1"), secret).json()["woken_workflows"] == []
    summary_only = _event("jira:issue_updated", "SUP-12", changelog={"items": [{"field": "summary"}]})
    assert "ignored" in _post(client, path, summary_only, secret).json()
    stale = json.dumps({"webhookEvent": "jira:issue_created", "timestamp": int(time.time() * 1000) - 3_600_000,
                        "issue": {"key": "SUP-13"}}).encode()
    assert _post(client, path, stale, secret).status_code == 400

    client.post("/settings/jira-webhook/delete", data={"csrf_token": token})
    assert _post(client, path, created, secret).status_code == 404


def test_status_and_label_changes_count(client, fake):
    from app.webhooks import jira_wake_reason

    status = {"webhookEvent": "jira:issue_updated", "issue": {"key": "SUP-3"},
              "changelog": {"items": [{"field": "status", "fromString": "To Do", "toString": "SFD validated"}]}}
    labels = {**status, "changelog": {"items": [{"field": "labels"}]}}
    assert jira_wake_reason(status) == "Jira issue SUP-3 changed" and jira_wake_reason(labels)
    assert jira_wake_reason({"webhookEvent": "worklog_created", "issue": {"key": "SUP-3"}}) is None
