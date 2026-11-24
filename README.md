# Maestro: project orchestrator for Upsun Dispatch

A web application (FastAPI, Jinja2, HTMX, SSE, PostgreSQL) where a team **describes a project** (what to deliver and how the work flows from the request to
production), lets Maestro design it, **reviews** the result, then **runs** it on Claude Managed Agents. Maestro works with your
tools (Linear, GitHub, Jira/Confluence) through **remote MCP servers** and hands coding work to
**Upsun Dispatch**. Each project is saved in *Projects* (the **Manage** menu); internally, it is stored as a Maestro workflow.
The application is ready to deploy on **Upsun**. The interface is in American English, in line with the Upsun brand guidelines, and so is this documentation.

## Maestro at a glance

Maestro lets you describe a project, the complete process from the request to production (steps,
approvals, loops, tools), and turns it into an executable **meta-workflow** that orchestrates Upsun
Dispatch, giving Dispatch every step it can do:

1. **Description → design.** On the home page (*Create*, "Describe your project"), you describe the project and how its
   work flows. Maestro (Claude, Messages API, structured output, `high` effort) generates:
   - the **orchestration prompt** followed on every run (mission, config, preflight, guardrails, ledger, steps,
     ticket and comment templates, end-of-run report);
   - the **steps and their actor**: Maestro, Upsun Dispatch (Issue to code, Code review, full review, CVE
     scan), people (gates) or Upsun Cloud;
   - the **assumptions**, what is **not possible with Dispatch** and the chosen alternative, the required
     tools and the config parameters (asked when the project is run).

   The designer relies on a Dispatch knowledge base (`app/maestro/knowledge.py`), with no built-in workflow
   template.
   **Required tools.** Before any generation, Maestro checks (with a short call) the tools it will have to use
   itself. If the description involves a connector that is not configured (for example Linear) or a tool with no
   connector in Maestro (GitLab, Slack, sending emails, and so on), **no prompt is created**: the project
   switches to "blocked" with the message "connect it in Settings, then try again". If a required connector
   is removed later, Run now is disabled.
   **Clarify.** After the tool check and before the design, Maestro asks at most 6 multiple-choice questions
   (recommended option first, plus "Other") when a decision materially changes the workflow or is needed to
   fit what Dispatch can do (tracker, who validates what, one PR per issue, completion signal, first run
   scope). The project shows "needs your answers"; the answers are passed to the designer as final
   decisions, shown with the design, and never asked again. A clear description goes straight to the design.
   **Editing.** *Edit* (workflow list) changes the name and the description; a changed description is
   designed again (tool check, Clarify, designer). Once the design is ready, the orchestration prompt can
   also be edited by hand there, until the next regeneration. *Try again* on a blocked or failed design
   shows the description so you can fix it first.
2. **Visualization.** A swimlane diagram (one column per actor, phases as bands, GATE badges,
   Dispatch workflow type, repetitions and loops), exportable as SVG.
3. **Launch.** *Run now* opens the settings of the run (prefilled with the last values; the owner can keep
   them for next time), then starts a run that you follow live. Projects are not scheduled: a run ends once
   Maestro has done its part, and the next one starts when you click *Run now* or when GitHub reports a
   merged pull request on the project's repository (GitHub wake-up), or when Linear or Jira reports a
   new or changed request (new issue, status or label change, comment) in the project's team or Jira project
   (Linear and Jira wake-ups). A Linear webhook covers a whole team, so Maestro only wakes the projects
   whose settings name the issue's Linear project (for example `LINEAR_PROJECT`: name, ID or URL); in the same
   way, Jira only wakes the projects whose settings name the Jira project (`JIRA_PROJECT`: key, name, ID or URL).
   They are set up in Settings. A change that arrives while a run is going is queued and starts a
   follow-up run when that run ends; Maestro's own tracker activity (recognized from its `MAESTRO-LEDGER`
   comments) never wakes a run.

**Following a run on its diagram.** Each run keeps a frozen copy of its workflow's design. On the run page,
a compact map of the steps stays visible while you scroll the transcript (left column, or a band at the top on
small screens). Maestro starts each message that moves to a step with a marker such as `[step: S2]` (hidden
in the transcript), so every message and tool call is attached to a step. At the bottom of the transcript the
map highlights the current step (past steps are dimmed); when you scroll back through the history, it
highlights the step of the messages you are reading.

**Triggering Dispatch, hands off.** Maestro does not call any Dispatch API (there is none
for Issue to code): it creates the ticket and posts the `@upsun-dispatch implement` comment through the
MCP connectors (GitHub, Linear, Jira), with **the user's credentials** (Dispatch ignores bots).
The Dispatch workflow then runs **exactly as Dispatch intends**: its human approvals
(plan approval, PR review and approval, merge) are done by people, in the tracker and
on the PR. Maestro never touches them (no comment, no fix, no retry, no decision, no merge). On
each run, it rereads the state of the tickets it started and compares it with the step's **completion signal** (PR merged,
PR approved, issue closed or PR opened); once it is reached, it moves on to the next step (for example
another Issue to code). A failed Dispatch run or a PR closed without merge is reported, not repaired. The state
lives in a "ledger" (an edited comment) on each tracked item.

## Contents

1. [Architecture](#architecture)
2. [BPMN 2.0 import](#bpmn-20-import)
3. [BPMN 2.0 export](#bpmn-20-export)
4. [Updating a project from a BPMN file](#updating-a-project-from-a-bpmn-file)
5. [Local setup](#local-setup)
6. [Deploying on Upsun](#deploying-on-upsun)
7. [Connector configuration](#connector-configuration)
8. [Security](#security)
9. [Tests](#tests)
10. [Known limitations](#known-limitations)

## Architecture

```
Browser ──HTMX/SSE──▶ web (FastAPI, uvicorn) ──▶ PostgreSQL ◀── worker (python -m app.worker)
                               │  replies, approvals,                       │  creates the session, sends the message,
                               │  cancellation                              │  reads the event stream,
                               ▼                                            ▼  stores run_events
                         Anthropic API (Managed Agents beta) ◀───────────────┘
```

| Module | Role |
|---|---|
| `app/auth` | First launch (`/setup`), login, logout, rate limiting of login attempts |
| `app/settings` | API key, model, MCP connectors, OAuth (discovery, DCR, PKCE), resource synchronization |
| `app/anthropic_client.py` | **All** the Managed Agents logic: agent, environment, vault, credentials, sessions, events |
| `app/workflows` | Workflow list, edit, delete, run, history; message composition (system instructions, then content) |
| `app/runs` | Run details, SSE stream, replies, tool approvals, cancellation, state machine |
| `app/worker.py` | Upsun worker: drives the sessions, resumes after a restart (claim + heartbeat) |
| `app/admin` | Users (creation, deletion, password reset) and System instructions |
| `app/webhooks.py` | `POST /webhooks/anthropic` (verified signature) for `vault_credential.refresh_failed` |

### Managed Agents resources per user

- **Agent**: model, system prompt, `mcp_servers` and one `mcp_toolset` per configured connector. No secrets. The built-in toolset (`agent_toolset_20260401`, set to `auto`) stays enabled, with `web_search` and `web_fetch` disabled.
- **Environment**: `cloud`, `limited` network with `allow_mcp_servers: true`.
- **Vault**: one per user. It holds the `static_bearer` or `mcp_oauth` credentials, bound to the exact MCP URL.
- Synchronization is **idempotent**. The fingerprint (SHA-256) of the agent configuration is stored, and the agent is only updated (new version) when it changes. IDs that have disappeared on the Anthropic side are recreated.

### Run lifecycle

1. The web app creates a `workflow_run` with the `running` status (Run now), or a GitHub, Linear or Jira wake-up starts one (merged pull request; new or changed request).
2. The worker claims it, creates the session (`agent`, `environment_id`, `vault_ids`), opens the stream, rereads the history (deduplication by event id), then sends the `user.message`.
3. Each event is stored in `run_events` and updates the status:
   - `session.status_idle` + `requires_action` gives **waiting for approval**. The blocking `event_ids` are stored.
   - `session.status_idle` + `end_turn` gives **waiting for your reply** if Claude's last message ends with a question, otherwise **completed**.
   - `retries_exhausted` gives **failed**. `session.error` is displayed; an `mcp_authentication_failed_error` error switches the connector to "Error".
4. The browser follows live through `/runs/{id}/stream` (SSE). The stream reads the database with short polling (1 s), after first displaying the events already stored.
5. Replies (`user.message`) and approvals (`user.tool_confirmation` allow/deny + `deny_message`) go from the web app directly to the API. The run is then handed back to the worker.

### Predicted plan (diagram)

Each run and each workflow shows a **Planned workflow** diagram: the main steps that Claude
expects to carry out, inferred from the request alone, before any execution.

- **Generation**: on first display, Maestro sends the request (the active system instructions followed by the
  workflow content) and the list of connectors to Claude (Messages API, model chosen in Settings, structured
  JSON output, `low` effort). One call per plan, billed to the user's key.
- **Interventions computed by the app**, never guessed by Claude: a call to a connector set to
  "Always ask" shows "Your approval", a question shows "Your reply", and a service that is not connected
  shows "Connector missing".
- **Cache**: a run's plan is frozen. A workflow's plan is recomputed when its content, the active system instructions,
  the connectors or the model change. A run started from a workflow reuses its plan.
- **Export**: "Download SVG" button; the file keeps the Upsun colors outside the application.
- The plan is a forecast: the transcript shows what actually happened.

### Workflow diagrams

On **Describe your project** you can give the process as a diagram instead of, or in addition to,
text: paste an image into the description field (Ctrl+V or Cmd+V), drop a file, or choose one
(PNG, JPEG, GIF or WebP up to 3.75 MB, or a PDF up to 10 MB). The file type is checked from its
content. The diagram is stored with the workflow (`workflows.diagram_data`) and sent to Claude with
the text for the tool check, the Clarify step and the designer, including when you regenerate the
design. Only its owner can open it (`/workflows/{id}/diagram`).

### System instructions

System instructions (admin menu **System instructions**, read-only for other users at `/instructions`)
are rules that apply to every workflow, for every user. They are stored in the `skills` table
(historical name).

- **They take precedence** over a workflow description. The tool check, the Clarify step and the
  designer all receive them: Clarify never asks what they already decide, the tool check counts the
  tools they require, and the designer rewrites the workflow to comply and lists each change under
  **Adjusted to system instructions** on the design page.
- **They are sent at the start of every run**, with the same precedence rule, and frozen in
  `workflow_runs.skills_snapshot` (shown as "System instructions applied" on the run page).
- **When an admin changes them**, existing designs are not regenerated automatically. Each design
  stores a fingerprint of the instructions it was generated with (`workflows.instructions_hash`);
  when it no longer matches, the workflow shows **instructions changed** and its design page offers
  **Regenerate the design**. Runs apply the new instructions right away.
- **Hierarchy**: Maestro's built-in Dispatch rules (hands off during Dispatch runs, separate trigger
  comment, connected tools only), then the system instructions, then the workflow description.

### Message sent for a workflow

```xml
<system_instructions>
System instructions set by the administrator of this application. They apply to every workflow, ... and they take precedence over the workflow that follows ...
<instruction name="INSTRUCTION NAME">INSTRUCTION TEXT</instruction>
</system_instructions>

<workflow name="WORKFLOW NAME">
WORKFLOW CONTENT
</workflow>
```

## BPMN 2.0 import
**One entry point: the Create page** ("Describe your project"). Its **Start from** choice offers *A description* and
*A BPMN file*. The **Import BPMN** buttons on the *Projects* page (next to "Describe a new project", and in the empty
state) open `GET /maestro/new?start=bpmn`, which preselects *A BPMN file*. Dropping or pasting a `.bpmn` file on the
Create page also switches to that mode. Both modes post to `POST /maestro/new` (CSRF, multipart, with the Private /
Shared choice); `start=bpmn` selects the import. The old `/maestro/import-bpmn` URLs redirect to the Create page in
BPMN mode.

In BPMN mode you give:
- the **Name** of the project. It is used as typed (the process name in the file is no longer used). A name you
  already use is not an error: it makes the file match your project, and you choose what to do (see *Existing
  project detection* below);
- the **BPMN file** (`.bpmn`);
- a required **Context** (at least 40 characters): why the project exists, who is involved, tools, constraints and
  what the diagram does not say. It is stored with the project, as `## Context from the user` before
  `## Imported BPMN process`, so the import review and the designer both read it.

Any error (name, context, file) returns to *Create* in BPMN mode with the name, context and Private /
Shared choice kept; choose the file again, since browsers do not keep file inputs.

### Existing project detection

Before creating anything, Maestro checks, with no LLM and no Anthropic call, whether the file matches one of **your
own** projects (projects shared with you are never candidates: updating requires ownership). Three rules apply in
order and the first hit wins; within a rule the project with the most recent design date (or the highest id) is taken:

1. **`exported`**: the file has a `maestro:design` block (changed or not) and its process id `Process_<id>` is the id
   of one of your projects ("It was exported from *project*.").
2. **`name`**: a project name equals, case-insensitively and trimmed, the name you typed or the process name in the
   file ("It has the same name as *project*.").
3. **`steps`**: the steps rebuilt from the file have the same fingerprint (`steps_hash`) as the steps of one of your
   `ready` designs ("It has the same steps as *project*.").

On a match nothing is created yet. The uploaded file, the name, the context and the Private / Shared choice are kept
in the `pending_bpmn_imports` table (migration `0013`) and you are sent to `/maestro/import-match/{id}`:
"This BPMN file looks like an existing project". The page offers two choices:
- **Update *project*** runs exactly the flow of *Import BPMN* on the project's page (see *Updating a project from a
  BPMN file*): same project, saved settings kept, runs untouched, the review dialog when needed. It is disabled
  while the design is being made or an import review is open. It is preselected for an `exported` match.
- **Create a new project** imports the file as a separate project, under a proposed name (for example "TMA (2)") that
  you can edit and must confirm. It is preselected for the `name` and `steps` matches.

*Cancel* drops the pending file and returns to the Create page. The file is parsed again, with the same parser and
2 MB limit, each time the page is shown or answered. A pending file expires after 1 hour (a 404 for its id); a user's
expired files are deleted the next time that user saves one, so a file abandoned by someone who never comes back stays
in the table. Without a match, the import creates the project directly, as before. *Import BPMN* on a project's page
never shows this choice.

A `.bpmn` file from Maestro or any BPMN tool is imported as a project. **Maestro does not execute BPMN**: the file is
an input to the design. No LLM ever reads or writes the XML; parsing is deterministic (`app/maestro/bpmn.py`).

**Safe parsing.** The file is parsed with `defusedxml`. A DOCTYPE, entities and external references are rejected,
the size limit is 2 MB, the root must be `definitions` in the BPMN 2.0 MODEL namespace
(`http://www.omg.org/spec/BPMN/20100524/MODEL`) and the file must contain at least one `process`. An invalid file
shows an error and creates no project.

**Two cases.**
- **A Maestro file that was not changed.** The `maestro:design` block holds the stored design and
  `maestro:stepsHash` matches the steps rebuilt from the BPMN elements (and the stored steps). The design is restored
  as is, status `ready`, with no review and no Anthropic call. The context you typed is kept with the project, so
  the next design reads it.
- **Any other file** (another tool, or a Maestro file edited since the export). The process becomes a structured
  English description (lanes as roles, tasks in flow order with type, lane, id and documentation, gateways and their
  conditions, loops, multi-instance markers, timer start) plus **deterministic findings**, one per element that
  cannot run as drawn. A new review step (one structured Messages API call, the model chosen in Settings, `high`
  effort) then returns at most 8 issues. Each has the problem, the Maestro or Dispatch limit that applies and 2 to 4
  alternative routes, the recommended one first. Every finding is always covered: when the review leaves one out, a
  generic issue "This BPMN element is not supported" is added (options: "Describe it as a step done by a person",
  "Leave it out", or your own).

**The review dialog.** When there are issues, the project is `needs_answers` (the same status as the Clarify
questions; the state is kept in `workflows.clarifications` with `kind = "import_review"`, no migration). For each
issue you see the element name and BPMN id, the problem, why, and the routes as radio buttons with the effect of
each, the first one selected and badged "recommended", plus "Other" with a text field. *Continue* stores each choice as
a **final decision** (`question`, `answer`, `element_id`) and runs the normal pipeline (tool check, Clarify, designer):
the tool check counts the tools the chosen routes need, Clarify never asks these again, and the designer keeps the
BPMN ids as step ids where a task maps to one step and lists every reviewed issue in "Not possible with Dispatch today"
with the chosen route. *Import a corrected file* replaces the description of the same project and reviews it again;
*Cancel import* deletes the project after a confirmation page. Without any issue, the dialog is skipped and the
design starts straight away.

**Supported subset.** One process (lanes become roles), user, service, call activity, manual, send, receive and plain
tasks, exclusive gateways, sequence flows (including loops back), multi-instance markers, a timer start event, and
intermediate and end events. A file written by Maestro also carries its design in the `maestro:` namespace
(`https://upsun.com/schema/maestro/1`).

**Unsupported elements** (each one produces a finding):
- boundary events (timer, message, error...), message flows and more than one participant (pool);
- event subprocesses and transactions;
- compensation (events and handlers);
- parallel and inclusive gateways;
- data objects and data stores (and their references);
- script and business rule tasks;
- signal and escalation events.

## BPMN 2.0 export

The design page has an **Export BPMN** button in the header, next to *Run now*, *Edit*, *Runs* and *Import BPMN*: `GET /workflows/{id}/design.bpmn` returns the
design as a BPMN 2.0 file with diagram interchange (DI), named `maestro-<project>.bpmn`. It opens in bpmn.io, Camunda
Modeler, Signavio and Bizagi with the lanes, tasks, gateways and flows laid out. Access is the same as for
`design.svg` (the owner, or anyone who can view a shared project; a design that is not ready gives a 404).

**An exchange and governance format, not an execution format.** Maestro is not a BPMN engine: the process has
`isExecutable="false"`. No LLM writes the XML and there is no Anthropic call: the file is a deterministic conversion of
the stored design (`design_to_bpmn()` in `app/maestro/bpmn.py`, built with the standard library's `ElementTree`).

| Maestro | BPMN 2.0 |
|---|---|
| Project | a `collaboration` with one `participant` and its `process` |
| Actor | one horizontal `lane` per actor used, in the SVG order (People, Maestro, Upsun Dispatch, Upsun Cloud, Other systems) |
| Person (`human`) | `userTask`; the gate owner is in the documentation and in `maestro:gateOwner` |
| Maestro, Upsun Cloud | `serviceTask` |
| Dispatch | `callActivity` with `calledElement` set to the Dispatch workflow (`issue_to_code`, `code_review`, `full_review`, `cve_scan`) |
| Other systems | `task` |
| Completion signal (`done_when`) | after the step, an `intermediateCatchEvent` with a conditional event ("PR merged", "PR approved", "issue closed", "PR opened") |
| Branches | an exclusive gateway after the deciding step (`<step>_split`), one flow per path named after its label, and a join (`<step>_join`) |
| Loop back (`loop_to`) | an exclusive gateway after the step (`<step>_loop`): "Yes" goes on, "No · <condition>" goes back to the step, under the lanes |
| Repeat | `multiInstanceLoopCharacteristics` (sequential) on the task, with the text in its documentation and in `maestro:repeat` |
| Phase | a `group` per phase band (as in the SVG) with a `categoryValue`, and `maestro:phase` on each task |
| Schedule | a timer start event whose `timeCycle` is the cron, when the project has one; otherwise a plain start event |

Task ids are the step ids (made valid XML names when needed: `3a` becomes `S_3a`); the generated elements have stable
ids (`Start`, `End`, `S2_done`, `S2_loop`, `S5_split`, `S5_join`, `Flow_<source>_<target>`, `Lane_<actor>`...). The
first line of a task's documentation is its detail.

**Restoring the design.** The process `extensionElements` hold `<maestro:design version="1" stepsHash="...">` with the
whole design (name, description, steps, settings, orchestration prompt) as JSON in a CDATA block, and
`<maestro:exported at="..." app="maestro"/>`. The `maestro:` namespace is `https://upsun.com/schema/maestro/1`.
Importing the file again, unchanged, restores the design as is (see *BPMN 2.0 import*). A step id that had to be
changed, a detail with several lines, a branch with a single path or a loop that goes back into a branch path
cannot be rebuilt exactly from the BPMN elements: such a file goes through the import review instead.

**Diagram.** `compute_layout()` in `app/maestro/render.py` is the one place that decides the order of the steps, the
phase bands, the branch paths and the loops. The SVG draws from it (its output is unchanged) and the BPMN diagram uses
the same structure in BPMN units: lanes are horizontal bands, a node's x follows the flow, the paths of a branch stack
in sub-rows of their lane, and loop routes run under the last lane. Shapes have the usual BPMN sizes.

## Updating a project from a BPMN file

On a project's page, **Import BPMN** and **Export BPMN** sit on the same line as *Run now*, *Edit* and *Runs*, so the
round trip happens where the project is managed: export the diagram, rework it in Camunda Modeler or Signavio, then
import it back. *Import BPMN* is for the owner only and is hidden while the design is queued or being generated;
*Export BPMN* shows once the design is ready (same access as before: the owner, or anyone who can view a shared
project).

*Import BPMN* opens `GET /workflows/{id}/import-bpmn`: a BPMN file, an optional **What changed** text, and an
**Import and update** button (`POST /workflows/{id}/import-bpmn`, multipart, with the CSRF token). The project stays
the same: id, name, sharing and run history are kept, and only the design is replaced. There is no new parser and no
new Anthropic call:

- **An unchanged Maestro export** is restored as it was, with no call. The settings you saved are kept for the keys
  that still exist and are empty in the restored design.
- **Any other file** goes through the same import review and designer as a new import. The settings saved so far are
  carried over (`clarifications["previous_config"]`, dropped once applied): the new design keeps their values for the
  keys it still has, through the review dialog and the questions too.
- **What changed** is put before the imported process in the description (`## Context from the user`, then
  `## Imported BPMN process`), so the review and the designer read both. Left empty, only the process is stored.
- An invalid file returns to the update page with the error and changes nothing. While the import review dialog is
  open, the dialog's own *Import a corrected file* form is used instead.

Runs already started keep the design they started with (`design_snapshot`): updating a project never touches them.
There is no undo: the previous design and the decisions made for it are replaced.

## Local setup

Prerequisites: Python 3.12 or later (3.14 recommended, as on Upsun) and a PostgreSQL database. No Docker.
Two options for the database:

- **Option A, recommended**: use the PostgreSQL service declared in `.upsun/config.yaml`, through an SSH
  tunnel opened by the Upsun CLI. The project must already have been deployed once. Preferably use a
  development environment (an Upsun branch), not production.

  ```bash
  upsun tunnel:single --relationship database
  ```

  The command prints a `pgsql://<user>:<password>@127.0.0.1:<port>/main` URL. Copy it as is into `DATABASE_URL`
  (the `pgsql://` prefix is accepted) and leave the terminal open.
- **Option B**: any PostgreSQL 14 to 18 installed locally, for example
  `postgresql://user:pass@localhost:5432/dispatch`.

```bash
python3.14 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
cp .env.example .env
```

Fill in `APP_SECRET_KEY`, `ENCRYPTION_KEY` and `DATABASE_URL` in `.env` (the commands to generate them are
given in the file), then load the variables and run the migrations:

```bash
set -a; . ./.env; set +a
```

```bash
alembic upgrade head
```

Start the web app and the worker in two terminals (after `set -a; . ./.env; set +a` in each):

```bash
uvicorn app.asgi:application --reload --port 8000
```

```bash
python -m app.worker
```

Open http://localhost:8000. On first launch, you are redirected to `/setup` to create the admin.

> Locally, `COOKIE_SECURE=false` is required over HTTP. On Upsun (HTTPS), it is `true`.

## Deploying on Upsun

Upsun builds and deploys the application from **`.upsun/config.yaml`**. There is no Dockerfile and no image: the application, the worker and the PostgreSQL service are all declared in this file.

| Item | Configuration |
|---|---|
| Application | `python:3.14`, dependencies installed by `pip install -r requirements.txt` (`build` hook) |
| Web | `python -m uvicorn app.asgi:application --port $PORT`; `/static` served directly |
| `runs` worker | `python -m app.worker` (same code, different command; inherits the database relationship) |
| Migrations | `python -m alembic upgrade head` in the `deploy` hook (runs on the web container only) |
| PostgreSQL | `database` service of type `postgresql:18`, declared under `services:`, linked to the app by `relationships: database:` |
| Database connection | built by the app from `PLATFORM_RELATIONSHIPS` (`database` relationship), with an encoded password |
| Route | `https://{default}/` to `app`, with a redirect from `www` |

### Steps

```bash
upsun project:create
```

Then define the **sensitive** variables at the project level (one command per variable), before the
first push:

```bash
upsun variable:create --level project --name env:APP_SECRET_KEY --value "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" --sensitive true --visible-build false
```

```bash
upsun variable:create --level project --name env:ENCRYPTION_KEY --value "$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" --sensitive true --visible-build false
```

Optional, for the Anthropic webhook:

```bash
upsun variable:create --level project --name env:ANTHROPIC_WEBHOOK_SECRET --value "whsec_..." --sensitive true --visible-build false
```

Then deploy:

```bash
git push upsun main
```

| Variable | Required | Role |
|---|---|---|
| `APP_SECRET_KEY` | yes | Session cookie signature (32 characters minimum) |
| `ENCRYPTION_KEY` | yes | Fernet key for encryption at rest (Anthropic API key, OAuth secrets). **Do not change it**: encrypted data would become unreadable |
| `ANTHROPIC_WEBHOOK_SECRET` | no | Webhook `whsec_xxxx` secret. Without it, `/webhooks/anthropic` returns 404 |
| `PUBLIC_BASE_URL` | no | Derived from the main Upsun route. Used to build the OAuth callback URLs |

### Troubleshooting: 502 Bad Gateway

A 502 means the web process is not responding. Check the logs:

```bash
upsun logs app --lines 100
```

```bash
upsun logs deploy --lines 100
```

- **Missing variables**: if `APP_SECRET_KEY` or `ENCRYPTION_KEY` is missing or invalid, the app does not crash.
  It returns **503** with a page that lists the commands to run. Create the variables, then run
  `upsun redeploy`. Variables created after a push only take effect on the next deployment.
- **Migrations**: the `deploy` hook runs `python -m alembic upgrade head`. An error at this step shows up in
  `upsun logs deploy`.

### Anthropic webhook (optional)

In the Claude Console, open **Manage → Webhooks** and add `https://<your-domain>/webhooks/anthropic`,
subscribed to `vault_credential.refresh_failed`. The affected connector then switches to "Error" with the message
"Reconnect needed".

## Connector configuration

Each user configures their connectors in **Settings**. Tokens are sent **directly to the
Anthropic vault**. The database only keeps the type, the URL, the credential ID, the last 4 characters and the
date. Before saving, the application checks the token with an MCP `initialize` handshake.
The "Test" button then checks that the credential is present in the vault (and validates the OAuth token through
`mcp_oauth_validate`), and that the MCP server responds.

Information checked on 11/13/2026:

| Service | MCP URL | Static token (`static_bearer`) | OAuth |
|---|---|---|---|
| **Linear** | `https://mcp.linear.app/mcp` | Personal API key: Settings → Account → Security & Access → Personal API keys. Recommended permissions: Read + Create issues + Create comments, limited to the relevant teams | OAuth 2.1 with dynamic client registration (DCR) |
| **GitHub** | `https://api.githubcopilot.com/mcp/` | Fine-grained PAT: Settings → Developer settings → Personal access tokens → Fine-grained tokens. Permissions: Contents, Issues, Pull requests (read, or write if needed) | No DCR: the admin creates a GitHub OAuth App and enters its client_id / client_secret |
| **Atlassian** (Jira, Confluence) | `https://mcp.atlassian.com/v2/mcp` | Only a **service account API key** (Bearer). The Atlassian admin must enable *Connectors → Atlassian MCP server → Authentication → API token* | **Recommended.** OAuth 2.1 with DCR (`offline_access` to get the refresh token) |

### OAuth mode

1. The admin opens **Settings → OAuth apps**:
   - for **Linear** or **Atlassian**, they click "Register automatically" (RFC 7591 dynamic registration);
   - for **GitHub**, they enter the client_id / client_secret of an OAuth App whose callback URL is the one shown (`https://<domain>/oauth/github/callback`).
2. Users then see "Sign in with" followed by the service name. The application discovers the authorization server (RFC 9728 / RFC 8414), uses PKCE S256 and the `resource` parameter, then exchanges the code.
3. It creates an `mcp_oauth` credential with a `refresh` block (`token_endpoint`, `client_id`, `token_endpoint_auth`). **Anthropic then refreshes the token** automatically.

### Tool permissions

For each connector, you choose between "Always ask" (`always_ask`, the default) and
"Always allow" (`always_allow`). This choice is applied to the `default_config.permission_policy` of the
`mcp_toolset`. Changing it creates a new version of the agent.

## Security

- Passwords hashed with **argon2** (10 characters minimum). A constant response time is kept when the login is unknown.
- Signed session cookie, `HttpOnly`, `Secure` and `SameSite=Lax`, valid for 12 h. It is invalidated if the password is reset.
- **CSRF**: per-session token, checked on every POST (hidden field or `X-CSRF-Token` header for HTMX).
- **Login rate limiting**: 5 failures per login, or 20 per IP, over 15 minutes.
- The Anthropic API key is **encrypted at rest** (Fernet) and never shown again: only the last 4 characters are visible. Admin OAuth secrets are encrypted the same way.
- No secret is ever logged. The `httpx` and `anthropic` loggers are set to WARNING.
- Isolation: a user can only access their own workflows and runs (404 otherwise). The Users page and editing System instructions are restricted to the admin.
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy` headers.

## Tests

```bash
pytest -q
```

The Anthropic API is replaced by a fake client (`tests/conftest.py`). The tests cover:

- first launch and admin creation;
- cookies, CSRF, login rate limiting and logout;
- permissions (admin pages, other users' workflows and runs);
- API key encryption and synchronization idempotence;
- MCP server declaration and permission policies;
- message composition (active system instructions, in order, before the content; frozen snapshot); system instructions passed to the tool check, Clarify and the designer; outdated designs flagged;
- handling of `requires_action` (approval and denial with a reason), `end_turn` (question or completed), cancellation, resumption by the worker and the SSE stream.

To run the same tests on PostgreSQL (dedicated database, emptied for each test, never the Upsun one):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/dispatch_test pytest -q
```

## Known limitations

- **BPMN import**: a Maestro file is restored as is only when its steps are unchanged; any edit sends it through the review. The routes chosen in the review are not written back to a BPMN file (the next export reflects the new design), and there is no in-app BPMN editor.
- **Updating from a BPMN file**: the new design replaces the old one (no undo), and runs already started keep their snapshot of the design they started with. Name and sharing are not changed from the update page (use *Edit*).
- **BPMN export**: the file is meant for exchange and governance, not for execution (`isExecutable="false"`). The layout is computed, not hand-drawn: very large designs with many parallel branch paths or loops give tall lanes, and there is no XSD validation in the tests: check a new kind of design by opening the file in a BPMN tool.
- **Question detection**: heuristic, the last line of Claude's message must end with "?". In any case, the user can continue the conversation.
- **Atlassian with a static token**: personal API tokens (Basic `email:token`) are not compatible with `static_bearer`. Use OAuth or a service account key.
- **The "Test" button** cannot redo the authenticated handshake for a static token, since the token is not kept. It checks the credential on the vault side and that the server is reachable. The full handshake happens when saving.
- **SSE through polling** of the database (1 s) rather than `LISTEN/NOTIFY`: it is simple and robust behind the Upsun proxy, with a slight latency.
- **Anthropic workspace**: vaults are shared across the workspace. Use one API key per dedicated workspace.
- **Vault recreation** (API key from another workspace, archived vault): static tokens must be entered again, and the affected connectors switch to "Error".
- Files produced by the agent in the sandbox (`/mnt/session/outputs`) are not displayed.
