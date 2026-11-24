"""Updating an existing project from a BPMN file, and the Import/Export BPMN buttons in the page header."""

from __future__ import annotations

import re

from app.db import SessionLocal
from app.maestro import service
from app.models import Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user
from tests.test_bpmn_import import CLEAN, CONTEXT, FOREIGN, REVIEW, _calls, _state, maestro_file
from tests.test_maestro import QUESTIONS, _connect, _create, _design, _setup

BUSY_FLASH = "Wait until the current design is finished."


def _ready_project(client, fake) -> int:  # type: ignore[no-untyped-def]
    """A designed project (the fake design has one setting, REPO) with saved settings, REPO and OLD."""
    wf_id = _create(client)
    _design(wf_id)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        design = dict(wf.design_json)
        design["config"] = [{"key": "REPO", "description": "GitHub owner/repo", "value": "acme/mine"},
                            {"key": "OLD", "description": "Gone soon", "value": "keep-me-not"}]
        wf.design_json = design
        db.commit()
    return wf_id


def _set(wf_id: int, **values) -> None:  # type: ignore[no-untyped-def]
    with SessionLocal() as db:
        for key, value in values.items():
            setattr(db.get(Workflow, wf_id), key, value)
        db.commit()


def _update(client, wf_id: int, xml: str | bytes | None, context: str = "", token: str | None = None):  # type: ignore[no-untyped-def]
    token = token or csrf(client, f"/workflows/{wf_id}/design")
    files = {"bpmn": ("process.bpmn", xml, "application/xml")} if xml is not None else None
    return client.post(f"/workflows/{wf_id}/import-bpmn", data={"csrf_token": token, "context": context},
                       files=files, follow_redirects=False)


def _header(page: str) -> str:
    return page.split('<div class="row-actions">', 1)[1].split("</header>", 1)[0]


def _positions(header: str, wf_id: int) -> list[int]:
    hrefs = [f'href="/workflows/{wf_id}/{tail}"' for tail in
             ("run", "edit", "history", "import-bpmn", "design.bpmn")]
    return [header.find(h) for h in hrefs]


# --- Header -----------------------------------------------------------------------------------------


def test_the_owner_sees_the_buttons_in_order_and_the_svg_row_has_no_bpmn(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/design").text
    header = _header(page)
    positions = _positions(header, wf_id)
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)
    assert "Import BPMN" in header and "Export BPMN" in header
    assert re.search(r'design\.bpmn"[^>]*\bdownload\b', header)
    assert "Download BPMN" not in page and "Download SVG" in page
    assert page.count(f'href="/workflows/{wf_id}/design.bpmn"') == 1


def test_a_viewer_of_a_shared_project_can_export_but_not_import_or_edit(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    _set(wf_id, shared=True)
    make_user("carol")
    client.cookies.clear()
    login(client, "carol")
    header = _header(client.get(f"/workflows/{wf_id}/design").text)
    run, edit, history, imp, export = _positions(header, wf_id)
    assert history >= 0 and export >= 0 and edit == -1 and imp == -1


def test_import_is_hidden_while_the_design_is_being_made(client, fake):
    _setup(client)
    wf_id = _create(client)  # queued
    for status in ("queued", "generating"):
        _set(wf_id, design_status=status)
        header = _header(client.get(f"/workflows/{wf_id}/design").text)
        _, _, _, imp, export = _positions(header, wf_id)
        assert imp == -1 and export == -1
    for status in ("needs_answers", "blocked", "failed"):
        _set(wf_id, design_status=status)
        header = _header(client.get(f"/workflows/{wf_id}/design").text)
        _, _, _, imp, export = _positions(header, wf_id)
        assert imp >= 0 and export == -1, status


# --- The update page ------------------------------------------------------------------------------


def test_the_owner_gets_the_update_page(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    r = client.get(f"/workflows/{wf_id}/import-bpmn")
    assert r.status_code == 200
    page = r.text
    for text in ("Import BPMN", "Update ", " from a BPMN file", 'name="bpmn"', 'accept=".bpmn,application/xml,text/xml"',
                 'name="context"', "What changed", "Optional: what you changed in the diagram and why.",
                 'name="csrf_token"', "Import and update", "Cancel", 'enctype="multipart/form-data"',
                 f'action="/workflows/{wf_id}/import-bpmn"', f'href="/workflows/{wf_id}/design"',
                 "This replaces the project&#39;s design. Settings you saved are kept for settings that still exist. "
                 "Runs already started keep the design they started with."):
        assert text in page or text.replace("&#39;", "'") in page, text


def test_others_get_a_404_on_the_update_page_and_post(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    _set(wf_id, shared=True)
    make_user("carol")
    client.cookies.clear()
    login(client, "carol")  # a viewer of the shared project
    assert client.get(f"/workflows/{wf_id}/import-bpmn").status_code == 404
    assert _update(client, wf_id, CLEAN, token=csrf(client, "/maestro/new")).status_code == 404
    client.cookies.clear()
    login(client, "carol")
    _set(wf_id, shared=False)
    assert client.get(f"/workflows/{wf_id}/import-bpmn").status_code == 404
    assert _update(client, wf_id, CLEAN, token=csrf(client, "/maestro/new")).status_code == 404
    assert _state(wf_id)[0] == "ready"


def test_the_page_and_the_post_wait_while_the_design_is_being_made(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    for status in ("queued", "generating"):
        _set(wf_id, design_status=status)
        r = client.get(f"/workflows/{wf_id}/import-bpmn", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
        r = _update(client, wf_id, FOREIGN)
        assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
        assert BUSY_FLASH in client.get(f"/workflows/{wf_id}/design").text
        with SessionLocal() as db:
            wf = db.get(Workflow, wf_id)
            assert wf.design_status == status and wf.design_json["config"][0]["value"] == "acme/mine"
            assert "Imported BPMN process" not in wf.description


def test_posts_need_a_csrf_token(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    r = client.post(f"/workflows/{wf_id}/import-bpmn", files={"bpmn": ("a.bpmn", CLEAN, "application/xml")})
    assert r.status_code == 403
    assert _state(wf_id)[0] == "ready"


# --- Update with an unchanged Maestro export ----------------------------------------------------------


def test_an_unchanged_export_restores_the_design_without_any_call(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    _set(wf_id, shared=True)
    token = csrf(client, f"/workflows/{wf_id}/design")
    run = client.post(f"/workflows/{wf_id}/run", data={"cfg:REPO": "acme/mine", "csrf_token": token},
                      follow_redirects=False)
    run_id = int(run.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        before = (db.get(Workflow, wf_id).name, db.get(WorkflowRun, run_id).design_snapshot)
    calls = (len(fake.design_calls), _calls(fake))

    # The stored design has an empty value for REPO and a setting the project does not have
    xml = maestro_file().replace('"value": "acme/shop"', '"value": ""')
    r = _update(client, wf_id, xml, context="Ignored for a restored export")
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert (len(fake.design_calls), _calls(fake)) == calls  # zero Anthropic calls
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert (wf.id, wf.name, wf.shared) == (wf_id, before[0], True)
        assert wf.design_status == "ready" and wf.content == "# Mission\nStored prompt."
        assert [c["key"] for c in wf.design_json["config"]] == ["REPO"]
        assert wf.design_json["config"][0]["value"] == "acme/mine"  # kept for the matching key
        assert wf.clarifications is None
        assert db.get(WorkflowRun, run_id).design_snapshot == before[1]  # the run keeps its design
        assert db.get(WorkflowRun, run_id).workflow_id == wf_id
    assert "Project updated from the BPMN file. Your design is restored." in client.get(
        f"/workflows/{wf_id}/design").text


def test_a_stored_value_in_the_export_is_not_overwritten(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    assert _update(client, wf_id, maestro_file()).status_code == 303
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == "acme/shop"


# --- Update with another file ------------------------------------------------------------------------


def test_a_foreign_file_with_a_context_queues_the_review_on_the_same_project(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    _set(wf_id, shared=True)
    before = _calls(fake)
    r = _update(client, wf_id, CLEAN, context=f"  {CONTEXT}  ")
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert _calls(fake) == before  # nothing is called by the request itself
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "queued" and wf.design_json is None and wf.content == ""
        assert wf.shared is True
        assert wf.description.startswith(f"## Context from the user\n{CONTEXT}\n\n## Imported BPMN process\n")
        assert "Review the request" in wf.description
        assert wf.clarifications["kind"] == "import_pending"
        assert wf.clarifications["previous_config"] == [{"key": "REPO", "value": "acme/mine"},
                                                       {"key": "OLD", "value": "keep-me-not"}]
    assert "Project updated from the BPMN file. Maestro is reviewing it." in client.get(
        f"/workflows/{wf_id}/design").text

    _design(wf_id)  # a clean file: the review finds nothing, the designer answers with the setting REPO only
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "ready"
        assert wf.design_json["config"] == [{"key": "REPO", "description": "GitHub owner/repo", "value": "acme/mine"}]
        assert "previous_config" not in (wf.clarifications or {})


def test_the_carried_settings_survive_the_review_dialog_and_the_questions(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    fake.review_response = REVIEW
    fake.clarify_response = QUESTIONS
    assert _update(client, wf_id, FOREIGN).status_code == 303
    _design(wf_id)
    status, clar = _state(wf_id)
    assert status == "needs_answers" and clar["kind"] == "import_review"
    assert [c["key"] for c in clar["previous_config"]] == ["REPO", "OLD"]

    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        choices = {q["id"]: (q["options"][0]["label"], "") for q in wf.clarifications["questions"]}
        assert service.answer_import_review(db, wf, choices) == []
    _design(wf_id)  # the clarify questions come next
    status, clar = _state(wf_id)
    assert status == "needs_answers" and clar["questions"] and "kind" not in clar
    assert [c["key"] for c in clar["previous_config"]] == ["REPO", "OLD"]

    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        answers = {q["id"]: "Whatever" for q in wf.clarifications["questions"]}
        assert service.answer_questions(db, wf, answers) == []
    _design(wf_id)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "ready"
        assert [(c["key"], c["value"]) for c in wf.design_json["config"]] == [("REPO", "acme/mine")]
        assert "previous_config" not in wf.clarifications and wf.clarifications["decisions"]


def test_without_a_context_the_description_is_the_process_only(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    assert _update(client, wf_id, FOREIGN, context="   ").status_code == 303
    with SessionLocal() as db:
        description = db.get(Workflow, wf_id).description
    assert "Context from the user" not in description and "Imported BPMN process" not in description
    assert "Triage the ticket" in description


def test_no_saved_settings_means_no_carry_over(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)  # the REPO setting has no value
    assert _update(client, wf_id, CLEAN).status_code == 303
    assert "previous_config" not in _state(wf_id)[1]


def test_an_invalid_file_goes_back_to_the_update_page_and_changes_nothing(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        before = (wf.description, wf.design_json, wf.content, wf.design_status, wf.clarifications)
    for xml in (None, b"not xml at all"):  # no file, then a file that is not BPMN
        r = _update(client, wf_id, xml)
        assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/import-bpmn"
        assert "flash error" in client.get(r.headers["location"]).text
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert (wf.description, wf.design_json, wf.content, wf.design_status, wf.clarifications) == before


def test_the_review_dialog_still_takes_a_corrected_file(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    fake.review_response = REVIEW
    assert _update(client, wf_id, FOREIGN).status_code == 303  # not in the dialog yet: an update
    _design(wf_id)
    assert _state(wf_id)[0] == "needs_answers"
    # The dialog has its own corrected-file form: no header button and no update page with a context field.
    header = _header(client.get(f"/workflows/{wf_id}/design").text)
    assert f"/workflows/{wf_id}/import-bpmn" not in header
    r = client.get(f"/workflows/{wf_id}/import-bpmn", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    r = _update(client, wf_id, CLEAN, context="ignored in the dialog")
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert "Corrected file imported." in client.get(f"/workflows/{wf_id}/design").text
    with SessionLocal() as db:
        assert "ignored in the dialog" not in db.get(Workflow, wf_id).description


def test_a_corrected_file_in_the_dialog_keeps_the_carried_settings(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    fake.review_response = REVIEW
    assert _update(client, wf_id, FOREIGN).status_code == 303
    _design(wf_id)
    assert _state(wf_id)[1]["kind"] == "import_review"
    fake.review_response = {"issues": []}
    assert _update(client, wf_id, CLEAN).status_code == 303  # a corrected foreign file
    assert [c["key"] for c in _state(wf_id)[1]["previous_config"]] == ["REPO", "OLD"]
    _design(wf_id)
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == "acme/mine"


def test_a_corrected_maestro_export_in_the_dialog_gets_the_carried_settings(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    fake.review_response = REVIEW
    assert _update(client, wf_id, FOREIGN).status_code == 303
    _design(wf_id)
    assert _update(client, wf_id, maestro_file().replace('"value": "acme/shop"', '"value": ""')).status_code == 303
    with SessionLocal() as db:  # a corrected Maestro export is restored with the saved value
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == "acme/mine"


def test_a_second_update_before_the_design_is_ready_keeps_the_carried_settings(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    fake.review_response = REVIEW
    fake.clarify_response = QUESTIONS
    assert _update(client, wf_id, FOREIGN).status_code == 303
    _design(wf_id)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        choices = {q["id"]: (q["options"][0]["label"], "") for q in wf.clarifications["questions"]}
        service.answer_import_review(db, wf, choices)
    _design(wf_id)
    status, clar = _state(wf_id)
    assert status == "needs_answers" and clar["questions"]  # design_json is None, settings are carried
    assert _update(client, wf_id, FOREIGN).status_code == 303  # a second update while the first is unfinished
    assert [c["key"] for c in _state(wf_id)[1]["previous_config"]] == ["REPO", "OLD"]
    _design(wf_id)  # the review dialog opens again
    assert _update(client, wf_id, maestro_file().replace('"value": "acme/shop"', '"value": ""')).status_code == 303
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == "acme/mine"
