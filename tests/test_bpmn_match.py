"""Create page, "A BPMN file": a file that matches one of the user's projects gets a choice page."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.maestro import service
from app.maestro.bpmn import ImportResult, bpmn_to_design, parse_bpmn, steps_from_bpmn, steps_hash
from app.models import PendingBpmnImport, User, Workflow, utcnow
from tests.conftest import csrf, login, make_user
from tests.test_bpmn_import import CLEAN, CONTEXT, _calls, _import, maestro_file
from tests.test_bpmn_update import _ready_project, _set
from tests.test_maestro import _setup

PROJECT = "Page migration"  # the name of the project made by tests.test_maestro._create


def _export(client, wf_id: int) -> bytes:  # type: ignore[no-untyped-def]
    r = client.get(f"/workflows/{wf_id}/design.bpmn")
    assert r.status_code == 200
    return r.content


def _pending_ids() -> list[str]:
    with SessionLocal() as db:
        return list(db.scalars(select(PendingBpmnImport.id)))


def _to_match(client, xml, **kw) -> str:  # type: ignore[no-untyped-def]
    r = _import(client, xml, **kw)
    assert r.status_code == 303 and r.headers["location"].startswith("/maestro/import-match/"), r.headers
    return r.headers["location"].rsplit("/", 1)[1]


def _choose(client, pid: str, **data):  # type: ignore[no-untyped-def]
    token = csrf(client, f"/maestro/import-match/{pid}")
    return client.post(f"/maestro/import-match/{pid}", data={"csrf_token": token, **data}, follow_redirects=False)


def _card_input(page: str, value: str) -> str:
    found = re.search(rf'<input[^>]*value="{value}"[^>]*>', page)
    assert found, f"no {value} option"
    return found.group(0)


def _wf(db, user_id: int, name: str, **kw) -> Workflow:  # type: ignore[no-untyped-def]
    values = {"description": "A project", "content": "", "design_status": "ready",
              "design_json": {"steps": [], "config": []}, **kw}
    wf = Workflow(user_id=user_id, name=name, **values)
    db.add(wf)
    db.commit()
    return wf


def _result(**kw) -> ImportResult:  # type: ignore[no-untyped-def]
    return ImportResult(**{"kind": "foreign", "name": "Nothing like it", "description": "", **kw})


# --- Parser -------------------------------------------------------------------------------------------------


def test_the_source_id_is_read_from_an_exported_file_changed_or_not():
    assert bpmn_to_design(maestro_file()).source_id == 7  # the fixture's process is Process_7
    changed = bpmn_to_design(maestro_file(title="Edited in a BPMN tool"))
    assert changed.kind == "foreign" and changed.source_id == 7


def test_a_foreign_file_has_no_source_id_and_the_fingerprint_is_the_steps_hash():
    assert bpmn_to_design(CLEAN).source_id is None
    xml = maestro_file()
    assert bpmn_to_design(xml).steps_fingerprint == steps_hash(steps_from_bpmn(parse_bpmn(xml)))
    assert bpmn_to_design(CLEAN).steps_fingerprint == steps_hash(steps_from_bpmn(parse_bpmn(CLEAN)))


def test_a_process_id_that_is_not_an_export_id_gives_no_source_id():
    assert bpmn_to_design(maestro_file().replace("Process_7", "Process_abc")).source_id is None


# --- Matching rules -----------------------------------------------------------------------------------------


def test_the_rules_apply_in_order_and_the_first_hit_wins(client, fake):
    uid = make_user("bob")
    steps = [{"id": "S1", "title": "One", "actor": "maestro"}]
    with SessionLocal() as db:
        user = db.get(User, uid)
        by_steps = _wf(db, uid, "By steps", design_json={"steps": steps})
        by_name = _wf(db, uid, "Same Name")
        exported = _wf(db, uid, "Exported")
        result = _result(name="same name", steps_fingerprint=steps_hash(steps), source_id=exported.id)
        hit = service.find_matching_project(db, user, result, "whatever")
        assert (hit.workflow.id, hit.reason) == (exported.id, "exported")
        result.source_id = None
        hit = service.find_matching_project(db, user, result, "whatever")
        assert (hit.workflow.id, hit.reason) == (by_name.id, "name")
        result.name = "Nothing like it"
        hit = service.find_matching_project(db, user, result, "whatever")
        assert (hit.workflow.id, hit.reason) == (by_steps.id, "steps")
        result.steps_fingerprint = None
        assert service.find_matching_project(db, user, result, "whatever") is None


def test_the_name_is_compared_trimmed_and_case_insensitive_for_the_typed_and_the_file_name(client, fake):
    uid = make_user("bob")
    with SessionLocal() as db:
        user = db.get(User, uid)
        wf = _wf(db, uid, "Client Support")
        assert service.find_matching_project(db, user, _result(), "  client SUPPORT ").workflow.id == wf.id
        assert service.find_matching_project(db, user, _result(name="CLIENT support"), "Other").workflow.id == wf.id
        assert service.find_matching_project(db, user, _result(name="  "), "   ") is None


def test_the_steps_rule_needs_a_ready_design(client, fake):
    uid = make_user("bob")
    steps = [{"id": "S1", "title": "One", "actor": "maestro"}]
    with SessionLocal() as db:
        user = db.get(User, uid)
        wf = _wf(db, uid, "Not ready", design_status="needs_answers", design_json={"steps": steps})
        result = _result(steps_fingerprint=steps_hash(steps))
        assert service.find_matching_project(db, user, result, "x") is None
        wf.design_status = "ready"
        db.commit()
        assert service.find_matching_project(db, user, result, "x").reason == "steps"


def test_a_file_without_steps_never_matches_by_steps(client, fake):
    uid = make_user("bob")
    with SessionLocal() as db:
        user = db.get(User, uid)
        empty = _wf(db, uid, "Empty design", design_json={"steps": []})
        result = _result(steps_fingerprint=steps_hash([]))
        assert service.find_matching_project(db, user, result, "x") is None
        assert service.find_matching_project(db, user, _result(name="empty design", steps_fingerprint=steps_hash([])),
                                             "x").reason == "name"  # the other rules still work
        assert service.find_matching_project(db, user, _result(source_id=empty.id), "x").reason == "exported"


def test_another_users_project_never_matches_even_when_shared(client, fake):
    bob = make_user("bob")
    carol = make_user("carol")
    steps = [{"id": "S1", "title": "One", "actor": "maestro"}]
    with SessionLocal() as db:
        theirs = _wf(db, carol, "Shared project", shared=True, design_json={"steps": steps})
        result = _result(name="Shared project", source_id=theirs.id, steps_fingerprint=steps_hash(steps))
        assert service.find_matching_project(db, db.get(User, bob), result, "Shared project") is None


def test_a_draft_without_a_description_is_not_a_candidate(client, fake):
    uid = make_user("bob")
    with SessionLocal() as db:
        _wf(db, uid, "Plain", description=None)
        assert service.find_matching_project(db, db.get(User, uid), _result(name="Plain"), "Plain") is None


def test_the_most_recent_design_wins_within_a_rule_then_the_highest_id(client, fake):
    uid = make_user("bob")
    now = utcnow()
    with SessionLocal() as db:
        user = db.get(User, uid)
        old = _wf(db, uid, "Twin", design_updated_at=now - timedelta(days=2))
        recent = _wf(db, uid, "twin", design_updated_at=now - timedelta(days=1))
        assert service.find_matching_project(db, user, _result(name="TWIN"), "x").workflow.id == recent.id
        recent.design_updated_at = None
        old.design_updated_at = None
        db.commit()
        assert service.find_matching_project(db, user, _result(name="TWIN"), "x").workflow.id == recent.id


def test_a_proposed_name_is_unique_among_the_users_projects(client, fake):
    uid = make_user("bob")
    with SessionLocal() as db:
        user = db.get(User, uid)
        assert service.proposed_name(db, user, " Fresh ") == "Fresh"
        _wf(db, uid, "TMA")
        _wf(db, uid, "TMA (2)")
        assert service.proposed_name(db, user, "TMA") == "TMA (3)"
        assert service.proposed_name(db, user, "") == "Imported BPMN process"


# --- Create → BPMN without a match ----------------------------------------------------------------------------


def test_no_match_imports_directly_without_a_pending_row(client, fake):
    _setup(client)
    r = _import(client, CLEAN)
    assert r.status_code == 303 and re.fullmatch(r"/workflows/\d+/design", r.headers["location"])
    assert _pending_ids() == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 1


# --- The choice page -------------------------------------------------------------------------------------------


def test_an_exported_file_shows_the_choice_with_update_preselected(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    xml = _export(client, wf_id)
    pid = _to_match(client, xml, name=PROJECT)  # the typed name is taken too: the proposal gets a number
    assert _pending_ids() == [pid]
    page = client.get(f"/maestro/import-match/{pid}").text
    assert "This BPMN file looks like an existing project" in page
    assert f"It was exported from <strong>{PROJECT}</strong>." in page
    assert f"Update {PROJECT}" in page and "Create a new project" in page
    assert f'href="/workflows/{wf_id}/design" target="_blank" rel="noopener"' in page
    assert "checked" in _card_input(page, "update") and "checked" not in _card_input(page, "create")
    assert f'value="{PROJECT} (2)"' in page and 'maxlength="128"' in page
    assert f'action="/maestro/import-match/{pid}/cancel"' in page


def test_a_typed_name_equal_to_a_project_gives_the_name_reason_with_create_preselected(client, fake):
    _setup(client)
    _ready_project(client, fake)
    pid = _to_match(client, CLEAN, name=PROJECT.upper())
    page = client.get(f"/maestro/import-match/{pid}").text
    assert f"It has the same name as <strong>{PROJECT}</strong>." in page
    assert "checked" in _card_input(page, "create") and "checked" not in _card_input(page, "update")
    assert f'value="{PROJECT.upper()}"' in page  # the typed name is free: it is the proposal


def test_the_file_name_matches_too(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    _set(wf_id, name="Clean flow")
    pid = _to_match(client, CLEAN, name="Something else")
    assert "It has the same name as <strong>Clean flow</strong>." in client.get(f"/maestro/import-match/{pid}").text


def test_the_steps_reason(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    xml = _export(client, wf_id).decode().replace(f"Process_{wf_id}", "Process_9999")
    xml = xml.replace(f'name="{PROJECT}"', 'name="Renamed elsewhere"')
    xml = re.sub(r'("name": ")' + PROJECT, r"\1Renamed elsewhere", xml)
    result = bpmn_to_design(xml)
    assert result.source_id == 9999 and result.name == "Renamed elsewhere"
    pid = _to_match(client, xml, name="Brand new")
    page = client.get(f"/maestro/import-match/{pid}").text
    assert f"It has the same steps as <strong>{PROJECT}</strong>." in page


def test_the_page_still_works_when_the_match_is_gone(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    with SessionLocal() as db:
        db.delete(db.get(Workflow, wf_id))
        db.commit()
    page = client.get(f"/maestro/import-match/{pid}").text
    assert "Create a new project" in page and "Update " not in page
    assert "checked" in _card_input(page, "create")
    r = _choose(client, pid, choice="update", match_id=wf_id)
    assert r.status_code == 400 and "The project to update is no longer there." in r.text
    assert "Choose an option." not in r.text and "checked" in _card_input(r.text, "create")
    assert _pending_ids() == [pid]


def test_renaming_the_matched_project_after_the_page_was_shown_is_refused(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, CLEAN, name=PROJECT)  # a foreign file: matched by the typed name
    assert f'name="match_id" value="{wf_id}"' in client.get(f"/maestro/import-match/{pid}").text
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        wf.name = "Renamed"
        db.commit()
        other_id = _wf(db, wf.user_id, PROJECT).id  # now this one matches the typed name
        before = dict(db.get(Workflow, other_id).design_json)
    r = _choose(client, pid, choice="update", match_id=wf_id)
    assert r.status_code == 400 and "The project to update has changed since this page was shown." in r.text
    assert f'name="match_id" value="{other_id}"' in r.text  # the page now shows the project that matches
    assert 'checked' not in _card_input(r.text, "update")
    assert _pending_ids() == [pid]
    with SessionLocal() as db:
        assert db.get(Workflow, other_id).design_json == before


def test_the_page_carries_the_matched_project_id_and_the_name_is_not_required(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    page = client.get(f"/maestro/import-match/{pid}").text
    assert f'<input type="hidden" name="match_id" value="{wf_id}">' in page
    name_input = re.search(r'<input[^>]*id="new-name"[^>]*>', page).group(0)
    assert "required" not in name_input and 'maxlength="128"' in name_input


def test_updating_without_or_with_a_wrong_match_id_is_refused(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    for data in ({}, {"match_id": wf_id + 1000}):
        r = _choose(client, pid, choice="update", **data)
        assert r.status_code == 400 and "has changed since this page was shown" in r.text
        assert "checked" not in _card_input(r.text, "update")  # even for an exported file: confirm again
        assert "checked" in _card_input(r.text, "create")
    assert _pending_ids() == [pid]
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "ready"
    assert _choose(client, pid, choice="update", match_id=wf_id).status_code == 303


# --- Update -----------------------------------------------------------------------------------------------------


def test_updating_an_unchanged_export_restores_the_same_project_without_any_call(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    _set(wf_id, shared=True)
    xml = _export(client, wf_id)
    calls = (len(fake.design_calls), _calls(fake))
    pid = _to_match(client, xml)
    r = _choose(client, pid, choice="update", match_id=wf_id)
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert (len(fake.design_calls), _calls(fake)) == calls
    assert _pending_ids() == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 1
        wf = db.get(Workflow, wf_id)
        assert (wf.name, wf.shared, wf.design_status) == (PROJECT, True, "ready")
        assert wf.design_json["config"][0]["value"] == "acme/mine"  # the saved setting is kept
    assert "Project updated from the BPMN file. Your design is restored." in client.get(
        f"/workflows/{wf_id}/design").text


def test_updating_with_another_file_queues_the_review_and_keeps_the_settings(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, CLEAN, name=PROJECT)
    before = _calls(fake)
    r = _choose(client, pid, choice="update", match_id=wf_id)
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert _calls(fake) == before and _pending_ids() == []
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert (wf.id, wf.name, wf.design_status) == (wf_id, PROJECT, "queued")
        assert wf.description.startswith(f"## Context from the user\n{CONTEXT}\n\n## Imported BPMN process\n")
        assert [c["key"] for c in wf.clarifications["previous_config"]] == ["REPO", "OLD"]
    assert "Project updated from the BPMN file. Maestro is reviewing it." in client.get(
        f"/workflows/{wf_id}/design").text


def test_the_update_card_is_disabled_while_the_project_is_busy_or_in_review(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    xml = _export(client, wf_id)
    cases = [({"design_status": "queued"}, "Wait until its current design is finished."),
             ({"design_status": "generating"}, "Wait until its current design is finished."),
             ({"design_status": "needs_answers", "clarifications": {"kind": "import_review"}},
              "It has an import review in progress: finish or cancel it first.")]
    for values, reason in cases:
        _set(wf_id, design_status="ready", clarifications=None)
        pid = _to_match(client, xml)
        _set(wf_id, **values)
        page = client.get(f"/maestro/import-match/{pid}").text
        assert "disabled" in _card_input(page, "update") and reason in page
        assert "checked" in _card_input(page, "create")  # exported, but update is not possible now
        r = _choose(client, pid, choice="update", match_id=wf_id)
        assert r.status_code == 400 and reason in r.text
        with SessionLocal() as db:
            assert db.get(Workflow, wf_id).design_status == values["design_status"]  # untouched
            assert db.get(PendingBpmnImport, pid) is not None


# --- Create -----------------------------------------------------------------------------------------------------


def test_creating_with_the_proposed_name_makes_a_new_project_with_the_context(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id), extra={"shared": "1"})
    r = _choose(client, pid, choice="create", new_name=f"  {PROJECT} (2)  ")
    assert r.status_code == 303
    new_id = int(r.headers["location"].split("/")[2])
    assert new_id != wf_id and _pending_ids() == []
    with SessionLocal() as db:
        new = db.get(Workflow, new_id)
        assert new.name == f"{PROJECT} (2)" and new.shared is True and new.design_status == "ready"
        assert new.description.startswith(f"## Context from the user\n{CONTEXT}\n\n")
        assert db.get(Workflow, wf_id).name == PROJECT
    assert "BPMN file imported. Your design is restored." in client.get(f"/workflows/{new_id}/design").text


def test_creating_a_private_project_from_a_foreign_file(client, fake):
    _setup(client)
    _ready_project(client, fake)
    pid = _to_match(client, CLEAN, name=PROJECT)
    new_id = int(_choose(client, pid, choice="create", new_name="Support flow").headers["location"].split("/")[2])
    with SessionLocal() as db:
        new = db.get(Workflow, new_id)
        assert new.shared is False and new.design_status == "queued"
    assert "BPMN file imported. Maestro is reviewing it." in client.get(f"/workflows/{new_id}/design").text


def test_a_taken_name_gives_a_400_and_keeps_the_entered_name(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    r = _choose(client, pid, choice="create", new_name=PROJECT)
    assert r.status_code == 400
    assert f"You already have a project named {PROJECT}." in r.text and f'value="{PROJECT}"' in r.text
    assert "checked" in _card_input(r.text, "create")
    assert _pending_ids() == [pid]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 1
    assert _choose(client, pid, choice="create", new_name="Free name").status_code == 303  # the page still works


def test_an_empty_or_long_name_gives_a_400(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    for bad in ("", "   ", "x" * 129):
        r = _choose(client, pid, choice="create", new_name=bad)
        assert r.status_code == 400 and "The name must be between 1 and 128 characters." in r.text
    assert _pending_ids() == [pid]


def test_any_other_choice_gives_a_400(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    for choice in ("", "delete", "UPDATE"):
        r = _choose(client, pid, choice=choice)
        assert r.status_code == 400 and "Choose an option." in r.text
    assert _pending_ids() == [pid]


# --- Cancel -----------------------------------------------------------------------------------------------------


def test_cancel_drops_the_file_and_goes_back_to_the_create_page(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    token = csrf(client, f"/maestro/import-match/{pid}")
    r = client.post(f"/maestro/import-match/{pid}/cancel", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maestro/new?start=bpmn"
    assert _pending_ids() == []


# --- Access ------------------------------------------------------------------------------------------------------


def _all_404(client, pid: str) -> None:  # type: ignore[no-untyped-def]
    token = csrf(client, "/maestro/new")
    assert client.get(f"/maestro/import-match/{pid}").status_code == 404
    assert client.post(f"/maestro/import-match/{pid}", data={"csrf_token": token, "choice": "create",
                                                            "new_name": "X"}).status_code == 404
    assert client.post(f"/maestro/import-match/{pid}/cancel", data={"csrf_token": token}).status_code == 404


def test_an_unknown_id_is_a_404(client, fake):
    _setup(client)
    _all_404(client, "00000000-0000-0000-0000-000000000000")


def test_another_users_pending_file_is_a_404_and_stays(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    make_user("carol")
    client.cookies.clear()
    login(client, "carol")
    _all_404(client, pid)
    assert _pending_ids() == [pid]


def test_an_expired_file_is_a_404_and_saving_purges_the_old_rows(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    xml = _export(client, wf_id)
    old = _to_match(client, xml)
    with SessionLocal() as db:
        db.get(PendingBpmnImport, old).created_at = utcnow() - timedelta(hours=2)
        db.commit()
    _all_404(client, old)
    fresh = _to_match(client, xml)  # saving deletes the expired rows
    assert _pending_ids() == [fresh]


def test_saving_purges_the_expired_rows_of_every_user(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    carol = make_user("carol")
    with SessionLocal() as db:
        db.add(PendingBpmnImport(id="carol-old", user_id=carol, xml=b"<x/>", name="", context="", shared=False,
                                 created_at=utcnow() - timedelta(hours=2)))
        db.add(PendingBpmnImport(id="carol-new", user_id=carol, xml=b"<x/>", name="", context="", shared=False,
                                 created_at=utcnow()))
        db.commit()
    fresh = _to_match(client, _export(client, wf_id))
    assert sorted(_pending_ids()) == sorted(["carol-new", fresh])


def test_a_user_keeps_only_the_newest_pending_files(client, fake):
    uid = make_user("bob")
    carol = make_user("carol")
    cap = service.PENDING_MAX_PER_USER
    with SessionLocal() as db:
        db.add(PendingBpmnImport(id="carol-1", user_id=carol, xml=b"<x/>", name="", context="", shared=False,
                                 created_at=utcnow()))
        db.commit()
        user = db.get(User, uid)
        ids = [service.save_pending_import(db, user, b"<x/>", f"n{i}", "", False) for i in range(cap + 2)]
        kept = set(db.scalars(select(PendingBpmnImport.id).where(PendingBpmnImport.user_id == uid)))
        assert kept == set(ids[-cap:])
        assert db.get(PendingBpmnImport, "carol-1") is not None  # another user's rows are not counted


def test_the_match_page_is_login_protected(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    client.cookies.clear()
    r = client.get(f"/maestro/import-match/{pid}", follow_redirects=False)
    assert r.status_code in (303, 401)


def test_every_post_requires_a_csrf_token(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    pid = _to_match(client, _export(client, wf_id))
    assert client.post(f"/maestro/import-match/{pid}", data={"choice": "update"}).status_code == 403
    assert client.post(f"/maestro/import-match/{pid}/cancel").status_code == 403
    assert _pending_ids() == [pid]
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "ready"


# --- Import from a project's page ----------------------------------------------------------------------------------


def test_importing_from_a_projects_page_never_shows_the_choice(client, fake):
    _setup(client)
    wf_id = _ready_project(client, fake)
    token = csrf(client, f"/workflows/{wf_id}/design")
    r = client.post(f"/workflows/{wf_id}/import-bpmn", data={"csrf_token": token, "context": ""},
                    files={"bpmn": ("p.bpmn", _export(client, wf_id), "application/xml")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    assert _pending_ids() == []
