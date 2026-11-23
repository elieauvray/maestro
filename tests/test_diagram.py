from __future__ import annotations

from app.db import SessionLocal
from app.maestro.diagram import DIAGRAM_ONLY_DESCRIPTION, detect_media_type, with_diagram
from app.models import Workflow
from tests.conftest import csrf, login, make_user
from tests.test_maestro import _design, _setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _step(sid, actor="maestro", **extra):  # type: ignore[no-untyped-def]
    return {"id": sid, "phase": "", "title": f"Title {sid}", "detail": f"Detail {sid}", "actor": actor,
            "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": "", **extra}


GOLDEN_DESIGNS = {
    "empty": {},
    "fake": {
        "summary": "Migrate each page through Dispatch with human gates.",
        "steps": [
            {"id": "S1", "phase": "Spec", "title": "Write the stage issue", "detail": "Scope and context",
             "actor": "maestro", "dispatch_workflow": "none", "gate_owner": "", "repeat": "per page", "loop": ""},
            {"id": "S2", "phase": "Spec", "title": "Generate the spec", "detail": "Issue to code run",
             "actor": "dispatch", "dispatch_workflow": "issue_to_code", "gate_owner": "", "repeat": "",
             "loop": "", "done_when": "pr_merged"},
            {"id": "S3", "phase": "Spec", "title": "Approve the spec PR", "detail": "",
             "actor": "human", "dispatch_workflow": "none", "gate_owner": "PO", "repeat": "", "loop": ""},
            {"id": "S4", "phase": "Tests", "title": "Run e2e on <preview>", "detail": "",
             "actor": "upsun", "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": ""},
        ],
    },
    "branches": {
        "summary": "Branches",
        "steps": [
            _step("B0", "human", branch="Left"), _step("B1", "dispatch", branch="Right", done_when="pr_opened"),
            _step("B2", "upsun", branch="Left"),
            _step("A1", "maestro", phase="One"), _step("A2", "human", gate_owner="PO", phase="One"),
            _step("A3", "external", branch="Yes", phase="Two"), _step("A4", "maestro", branch="No", phase="Two"),
            _step("A5", "dispatch", branch="Maybe", phase="Two", dispatch_workflow="code_review"),
            _step("A6", "maestro", branch="Yes", phase="Two"),
            _step("A7", "upsun", phase="Three"),
        ],
    },
    "loops": {
        "summary": "Loops",
        "steps": [
            _step("L1", "maestro", phase="Build"), _step("L2", "dispatch", loop="tests fail", loop_to="L1",
                                                         dispatch_workflow="issue_to_code"),
            _step("L3", "human", loop_to="L1", gate_owner="PO"), _step("L4", "upsun", loop="no label", loop_to="ZZ"),
            _step("L5", "maestro", branch="Fast", loop="not green yet", loop_to="L5"),
            _step("L6", "upsun", branch="Slow"), _step("L7", "maestro", branch="Slow", loop="retry", loop_to="L6"),
            _step("L8", "human", repeat="for each page", loop="rejected", loop_to="L2"),
        ],
    },
    "escaping": {
        "summary": "Tom & <Jerry> \"quotes\"",
        "steps": [
            _step("E1", "maestro", phase="Phase <1> & more", title="A <b>bold</b> & 'quoted' title",
                  detail="x" * 200),
            _step("E2", "weird-actor", title="y" * 120, repeat="none", loop="n/a"),
        ],
    },
}
# SHA-256 of str(render_design_svg(design)) recorded from the renderer before compute_layout() was extracted.
GOLDEN_SVG_HASHES = {
    "empty": "b7b8093e91c21702025208a18340a6c65de7be314ed44b14966cf3caf5fac8fd",
    "fake": "debb5c08823cbdb4ffab96beaf7a786e65364294d8a220a0d82c705228d0e80f",
    "branches": "7c2c12d1a8ba4870d860c59529a37f5f725c4a8966767f2bef616c3166c27609",
    "loops": "b920e78cfee37c7061120fed3b3e88fdb8bd01a497a0dbf49c0bf47f589dd28c",
    "escaping": "bc64f111378c87a6b63f923a7154e129702325b3f1df58c2c9ac2b66794ad358",
}


def test_the_svg_diagram_renders_exactly_as_before_compute_layout():
    import hashlib

    from app.maestro.render import render_design_svg

    for name, design in GOLDEN_DESIGNS.items():
        digest = hashlib.sha256(str(render_design_svg(design)).encode()).hexdigest()
        assert digest == GOLDEN_SVG_HASHES[name], name


def _post(client, files, description=""):  # type: ignore[no-untyped-def]
    token = csrf(client, "/maestro/new")
    return client.post("/maestro/new", data={"name": "From a diagram", "description": description, "csrf_token": token},
                       files=files, follow_redirects=False)


def test_a_diagram_alone_is_enough_and_reaches_every_design_call(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("pasted-diagram.png", PNG, "image/png")})
    assert r.status_code == 303
    wf_id = int(r.headers["location"].split("/")[2])
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.description == DIAGRAM_ONLY_DESCRIPTION and wf.diagram_media_type == "image/png"
    _design(wf_id)
    stages = {stage: d for stage, d in fake.diagrams_seen}
    for stage in ("tools", "clarify", "design"):
        assert stages[stage]["media_type"] == "image/png" and stages[stage]["data"] == PNG, stage
    page = client.get(f"/workflows/{wf_id}/design").text
    assert f'href="/workflows/{wf_id}/edit"' in page and "Revise the design" not in page
    edit = client.get(f"/workflows/{wf_id}/edit").text
    assert "Current diagram" in edit and f'src="/workflows/{wf_id}/diagram"' in edit
    img = client.get(f"/workflows/{wf_id}/diagram")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG


def test_invalid_or_missing_input_is_refused(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("evil.png", b"<script>alert(1)</script>", "image/png")}, "x" * 50)
    assert r.status_code == 400 and "must be a PNG, JPEG, GIF or WebP image, or a PDF" in r.text
    r = _post(client, {"diagram": ("big.png", PNG + b"\x00" * 3_800_000, "image/png")}, "x" * 50)
    assert r.status_code == 400 and "too large" in r.text
    r = _post(client, {}, "too short")
    assert r.status_code == 400 and "or add a diagram" in r.text


def test_only_the_owner_sees_the_diagram(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})
    make_user("eve")
    login(client, "eve")
    assert client.get(f"/workflows/{wf_id}/diagram").status_code == 404


def test_diagram_content_blocks():
    assert detect_media_type(b"%PDF-1.7 ...") == "application/pdf"
    assert detect_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    blocks = with_diagram("text", {"media_type": "application/pdf", "data": b"%PDF-1.7"})
    assert blocks[0]["type"] == "document" and blocks[1]["type"] == "text" and blocks[1]["text"].endswith("text")
    assert with_diagram("text", None) == "text"


def test_text_overrides_the_diagram_in_every_prompt():
    from app.maestro.diagram import with_diagram
    from app.maestro.knowledge import TOOLS_SYSTEM

    content = with_diagram("Replace jira with linear", {"media_type": "image/png", "data": b"\x89PNG"})
    note = content[1]["text"]
    assert "takes precedence over the diagram" in note and note.endswith("Replace jira with linear")
    assert "A replaced tool is neither needed nor unsupported" in TOOLS_SYSTEM
    assert "comment that mentions them in the tracker" in TOOLS_SYSTEM


def test_edit_replaces_or_removes_the_diagram_and_redesigns(client, fake):
    from tests.conftest import csrf

    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/edit")
    pdf = b"%PDF-1.4\n%fake"
    r = client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                      "csrf_token": token},
                    files={"diagram": ("new.pdf", pdf, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.diagram_media_type == "application/pdf" and wf.diagram_data == pdf and wf.design_status == "queued"
    _design(wf_id)
    assert fake.diagrams_seen[-1][1]["media_type"] == "application/pdf"

    # Removing the only input is refused; removing it with a real description works.
    token = csrf(client, f"/workflows/{wf_id}/edit")
    r = client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                      "remove_diagram": "1", "csrf_token": token})
    assert r.status_code == 400 and "or add a diagram" in r.text
    text = "Fix bugs: Maestro files each bug as a GitHub issue and Dispatch implements it, the lead merges."
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": text,
                                                  "remove_diagram": "1", "csrf_token": token})
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.diagram_data is None and wf.description == text and wf.design_status == "queued"


def test_edit_change_request_is_sent_as_feedback(client, fake):
    from tests.conftest import csrf

    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/edit")
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                  "feedback": "Use Linear", "csrf_token": token})
    _design(wf_id)
    assert fake.design_calls[-1]["feedback"] == "Use Linear"
