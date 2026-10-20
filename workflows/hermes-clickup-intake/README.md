# ClickUp planning intake for Hermes

Bring new ClickUp requests into an existing **Hermes Kanban** development workflow. A small, deterministic Python poller handles discovery, comments, attachments, approvals and progress. Hermes workers do the planning and reviewed development; unchanged polls make no model calls.

```text
New ClickUp task → 3-minute quiet period → Hermes planner ↔ questions on ClickUp
                                              ↓
                                    independent plan review
                                              ↓
ClickUp: “Approve plan r1 for development” → orchestration → implementation/reviews
                                              ↓
                                 exact-candidate E2E → ticket check
                                              ↓
                                         dev + package
```

**This is a Windows, self-hosted Hermes integration, not a hosted ClickUp app or a one-click skill install.** It preserves the existing chat-based intake. Public-source preparation does not install anything, activate polling, create a ClickUp task or change a running Hermes instance.

## What is included

- The poller, durable SQLite state/outbox, ClickUp transport and strict approval parser.
- Worker templates for planning, orchestration, an approval gate and the final promotion check.
- Optional human-decision questions from development/review workers back to the originating ClickUp task, with generation-bound answers, answer fences and a bounded parking protocol.
- Credential-shaped outbound-text filtering with bounded correction feedback to the planner; canonical and compatible structured promotion receipts.
- Plan-publication recovery from an explicit, artifact-bound reviewer PASS when a planner used legacy request metadata; current-input and development-approval checks remain in place.
- Original regression tests with synthetic identities instead of deployment data.
- An **offline preparation tool** that substitutes your paths, IDs, approvers and planning skills into a new staging directory, leaving the supplied runtime algorithms intact.
- A sample installation configuration containing deliberately unusable example IDs.

No live configuration, credentials, task history, private project skills, state database or internal planning documents are included. The legacy entry name `clickup_dedicated_panel_intake.py`, runtime directory `clickup-dedicated-panel`, and `clickup-dp:` idempotency prefix are retained for source continuity; they do not select a project. The configured board/list and your worker instructions do.

## Requirements and compatibility

1. **Windows** with Python. Windows current-user DPAPI protects the ClickUp token; the poller also uses `msvcrt` locking and Windows process identity APIs. Linux/macOS runtime support is not implemented. The runtime uses the Python standard library only.
2. **A compatible Hermes installation with Kanban and no-agent cron.** The originating deployment was audited against Hermes commit `1fd18277267152893a8a24792b79213e7ca3f899` (`rc.13-v0.21.7`). This is a tested source reference, not a claim that any upstream release with a similar version has identical contracts. Run the supplied real-CLI tests against your installation first. The optional question relay also requires native dependency waits, scratch question/fence/park cards and the exported run/event fields described in the protocol. See [compatibility contracts](docs/protocol.md#hermes-contracts).
3. Your own Kanban board and `planner`, `plan-reviewer`, `orchestrator`, implementation, review, E2E and integration profiles. Configure model credentials and the dispatcher separately. The [other Hermes workflow in this repository](../hermes-approval-gated-kanban/README.md) explains the overall topology but is a separately dated snapshot, not an installer or guaranteed configuration match.
4. A **project-specific approved development workflow**. Workers must know the repository, safe worktree policy, test requirements, independent reviews, `dev` promotion, package contents and final cleanup. This package does not grant deployment authority or supply private project instructions.
5. One ClickUp list, an account/token for the poller, known numeric ClickUp user IDs for approvers, and a Discord channel for passive progress notices if enabled. See [ClickUp authentication](https://developer.clickup.com/docs/authentication). This implementation uses a locally provisioned token; it has no OAuth onboarding or token-refresh service. It is not a multi-tenant application.

Executable paths must not contain whitespace or backticks: the preserved promotion-command renderer emits unquoted paths. Use a suitable installation path rather than assuming a path containing spaces will work.

## 1. Prepare the ClickUp list

Use these **exact status name/type pairs**. Configure their inheritance in ClickUp yourself; the integration does not create or rename statuses.

| Name | API type |
|---|---|
| `reviewing feature` | `open` |
| `business requirements` | `unstarted` |
| `technical requirements` | `unstarted` |
| `waiting for approval` | `unstarted` |
| `implementing` | `custom` |
| `implemented` | `done` |
| `ready for deployment` | `done` |
| `deployed` | `closed` |

The preflight compares the **whole set**, including types. A mismatch stops task reads, external effects and ticket renewal. Do not merely change `expected_statuses` to accept a different vocabulary: the engine also writes these status names.

The enforced task scope is the task's home `list_id`. Workspace, space and folder IDs are recorded configuration, not additional hierarchy authorization checks. Do not expose the poller account to unrelated lists unnecessarily.

**Plain-comment calibration:** the sample configuration now includes `"plain_attr_allowlist": ["block-id"]`. ClickUp was observed attaching this neutral editor key to a newline in an otherwise plain closure/approval. An empty allowlist rejects that representation as unknown. Existing installations using the same shape should add only `block-id` to their private configuration; no parser-code update is needed. Quoted/code-formatted, edited, unauthorized and stale approvals remain rejected. Do not broadly allow arbitrary formatting attributes; see [calibration and regression coverage](docs/protocol.md#plain-comment-calibration).

## 2. Create private installation settings

From this directory, copy `installation.example.json` **outside the Git checkout**, then edit it. Replace every example ID and path. Never put a token in this file.

```powershell
Copy-Item installation.example.json C:/Private/clickup-installation.json
```

Set:

- `hermes_home`: the intended Hermes profile home, not its source-code checkout.
- `hermes_exe` / `python_exe`: existing compatible executables. Use the interpreter you will actually run under cron and in promotion cards.
- `credential_path`: the future private DPAPI file; preparation never opens it.
- `discord_guild_id`: server ID; `config.discord_chat`: channel ID. These are distinct from ClickUp user IDs.
- `request_skills`: installed planning skills accessible to the planner profile. Public defaults use `plan` and `pragmatic-code`; add your own project skill if needed. No private project skill is supplied.
- `config`: your ClickUp IDs, bot API user ID, a **nonempty** approver map, board name and any supported tuning. Use numeric-string keys for approvers, for example `{"123456": "Project owner"}`. Display names are labels; the numeric ID controls authority.

Prepare a **new, absent directory outside both the checkout and your Hermes home**. Create its parent directory first:

```powershell
python tools/prepare.py --settings C:/Private/clickup-installation.json --output C:/Staging/clickup-intake
```

The tool validates configuration and source substitution, produces a hash manifest, and refuses an existing destination. It does **not** call Hermes or ClickUp, read credentials, install files or create a scheduler job. Initial configuration is always disabled/shadow, with notifications, automatic handoff and automatic gate completion off, and `cutover_ms: null`. The optional question relay also starts off: its separate activation timestamp lives in the state database, which preparation does not create.

Generated layout:

```text
C:/Staging/clickup-intake/
  scripts/
    clickup_dedicated_panel_intake.py
    clickup_intake/*.py
    templates/*.md
  integrations/clickup-dedicated-panel/config.json
  manifest.json
```

The generated files contain your private deployment metadata. Do not commit or share the staging directory. Review the templates and settings against your own project policy before installing. The preparation tool changes only deployment constants; it does not implement arbitrary changes to the approval protocol.

## 3. Install disabled and provision the token

Back up your own deployment first. For a **new** installation, copy the generated `scripts` contents and integration directory into the same relative locations under `hermes_home`. Check for collisions first: do not overwrite an existing `templates` file, state database, integration configuration or poller. The templates must sit alongside the package; moving them breaks imports. Include `blocker_triage.md` and `question_protocol.md` as well as the original templates.

**This flat-layout copy procedure is not an in-place upgrade.** The supplied entry resolves configuration from its installation root, not the current working directory. An existing entry may instead select an immutable release; replacing its adjacent, unversioned files would then leave the active code unchanged. Before an authorized upgrade, resolve the entry's actual imported package/template paths and explicit configuration root, preserve local bindings and state, and test a separate copy. This package supplies neither a release switcher nor an automatic upgrade/rollback installer; do not overwrite a live release with prepared files.

The token file must be ASCII hex **Windows current-user DPAPI** ciphertext of the UTF-16LE token. Under the **same Windows account that runs the poller**, use Windows PowerShell's masked prompt and default current-user protection (do not use `-Key`):

```powershell
$credentialFile = 'C:/Private/clickup/api-key.dpapi' # match credential_path
New-Item -ItemType Directory -Force (Split-Path $credentialFile) | Out-Null
if (Test-Path $credentialFile) { throw 'Credential already exists; do not overwrite it blindly.' }
$secureToken = Read-Host 'ClickUp API token' -AsSecureString
$secureToken | ConvertFrom-SecureString | Set-Content -Encoding Ascii -Path $credentialFile
Remove-Variable secureToken
```

Restrict directory/file access to the poller identity and administrators. Do not enter a token in a shell argument, this repository, an agent conversation or a shared log. The runtime reads this encrypted file; it does not accept a token environment variable. Switching the Windows service account requires reprovisioning access under the intended identity.

With `enabled: false`, this command must exit successfully and silently; it does not contact ClickUp or initialize state:

```powershell
C:/Hermes/hermes-agent/venv/Scripts/python.exe C:/Hermes/scripts/clickup_dedicated_panel_intake.py
```

Use your actual configured paths in every command below.

## 4. Verify before enabling

Run the public tests as described below. Separately validate your actual board/profiles and test list using your organization's permitted live-test procedure.

For an authorized read-only external probe, set the installed `config.json` to `enabled: true`, keep `mode: "shadow"`, and run:

```powershell
C:/Hermes/hermes-agent/venv/Scripts/python.exe C:/Hermes/scripts/clickup_dedicated_panel_intake.py run --mode shadow
```

**Shadow is not offline or stateless.** It reads ClickUp and Kanban, writes observations/pending effects/staging/logs locally, but does not send external write effects. Reusing that state in live mode can execute queued work. Use a separate disposable installation for experiments; never delete production state to reset a test. A disabled run is not a preflight success.

For production activation, establish a deliberate cutoff timestamp in milliseconds, enable the intended handoff/notification/gate settings, then select `enabled: true`, `mode: "live"`. Use a fresh production state after disposable testing or explicitly account for already observed tasks. Tasks baselined while `cutover_ms` was null are not retroactively admitted just by changing the cutoff; `admit <task>` is the explicit exception.

Decide these **effectful settings** explicitly:

- `handoff_unblock: true` lets the poller release the approved orchestrator card for development. False disables only that handoff, not planner pickup or conversation wakes; polling with `enabled: true` and `mode: "live"` still permits planning.
- `auto_gate: true` lets the integration complete a valid approval gate after E2E; false requires the operator to **complete** it. Archiving a gate is not equivalent to a valid ticket.
- `notify_mode: "notify"` enables passive Discord progress subscriptions; it does not wake the channel's conversational agent. `off` creates no such subscription.

### Optional: enable human-decision questions

This is separate from `config.json`'s `enabled`, intake `cutover_ms`, handoff and notification settings. After verifying the worker contract and obtaining authority to activate the relay, use the installed entry:

```powershell
C:/Hermes/hermes-agent/venv/Scripts/python.exe C:/Hermes/scripts/clickup_dedicated_panel_intake.py blockers enable
# Optional, separately authorized adoption by existing eligible work cards:
C:/Hermes/hermes-agent/venv/Scripts/python.exe C:/Hermes/scripts/clickup_dedicated_panel_intake.py blockers protocol-comment
```

`enable` records the current epoch milliseconds as `meta.blocker_cutover_ms` in this integration's SQLite database. There is no `blockers_enabled` JSON option; unknown config keys are rejected. Re-running `enable` replaces the cutoff, so do not use it as a routine status check. Only newly created, valid protocol cards at/after that cutoff are eligible; historical question cards are not backfilled.

New orchestrator bodies rendered while the feature is enabled require every descendant/amendment card to carry the question protocol. `protocol-comment` instead queues one idempotent protocol comment for each currently eligible, non-done/non-archived watched card of an unheld, nonterminal ClickUp task, never the approval gate. It does not rewrite card bodies or prove that a running worker adopted the instructions. Both commands take the integration lock and can initialize local state even when polling is disabled; neither reads the ClickUp token. Queued comments require a subsequent enabled **live** scan to be sent. See [worker questions and answers](docs/protocol.md#human-decision-question-relay) before enabling.

## 5. Schedule one scan per minute

Use the intended Hermes profile and inspect its CLI help before creating a job. This is a **script-only, no-agent** job; the entry interprets no arguments as `run`.

```powershell
hermes cron create "* * * * *" --name "clickup-planning-intake" --script "clickup_dedicated_panel_intake.py" --no-agent --deliver local --paused
hermes cron list --all
# Only after your activation checks and explicit approval:
hermes cron resume <job-id>
```

The script path is relative to `<hermes_home>/scripts`. `--deliver local` keeps poller alerts local; configure an explicit project destination if you want alerts delivered to chat. Poller alert delivery and passive **card status** subscriptions are separate. Do not create a second job if one already exists. The gateway/scheduler must be running under the DPAPI-capable identity. See [Hermes cron documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron).

## Daily use

1. Create a **new task** in the configured list in `reviewing feature`. Describe the request and attach supporting files.
2. Leave it unchanged for three minutes. Edits, human comments and uploads restart the quiet period; the bot's own comments do not.
3. Answer planning questions on the same task. Use a real ClickUp **@mention of the bot** for follow-up questions; typing its name as plain text is not a mention.
4. When requested, post a new plain comment with `Business requirements closed`. The requester or an approver may do this. `Technical requirements closed` requires an approver. Neither command approves development.
5. Review the published, independently reviewed plan attachment. A configured approver authorizes that exact revision by posting a **new top-level plain comment**:
   ```text
   Approve plan r1 for development
   ```
6. Follow status on the task. With the optional relay enabled, later development/review questions can also appear here. Answer them and post the requested fresh business/technical closure; an answer is not authority to change the approved scope or waive a security boundary. “Answer recorded” confirms delivery, while the separate resumption notice requires worker evidence.
7. Implementation still goes through independent review, exact-candidate E2E and the promotion ticket. Approval covers the adopted `dev`/package workflow, **never** a `master`/`main` release or production deployment. A completed promotion normally reports top-level `promoted_sha`; a compatible older structured receipt is also recognized under the [exact receipt rules](docs/protocol.md#promotion-receipt-compatibility).

Quoted, edited, stale, unrecognized or unauthorized approvals do not count. New material input can invalidate approval and hold promotion. Once work is at a done-type status, open a new task for additional scope. See [protocol and worker contracts](docs/protocol.md).

## Operations and recovery

Prefix these with `<python_exe> <installed-entry>`:

| Command | Effect |
|---|---|
| `status` | Read local phase/outbox/ticket counts; no credential read. Before initialization, `state unavailable` is expected. |
| `run [--mode shadow]` | One scan, not a long-running daemon. Live mode comes from JSON; `--mode live` is not supported. |
| `admit <task-id>` | API preflight/task read and explicit local admission of an eligible existing task. |
| `hold <task-id>` | Set an operator hold and revoke live tickets. |
| `release <task-id>` | Clear that hold; does not directly unblock a worker or restore a revoked ticket. |
| `blockers enable` | Set/replace the separate question-relay cutoff in local database metadata; does not enable polling. |
| `blockers protocol-comment` | While relay is enabled, read eligible Kanban cards and queue protocol comments for existing work; no immediate external write. |
| `blockers disable` | Stop new question detection/relay staging and queue its controlled drain/alerts; does not stop running workers or ordinary intake. |
| `blockers rollback-preflight` | Check relay-specific rollback blockers under the lock; exit 0 with `rollback preflight OK`, otherwise exit 1 with reasons. |
| `outbox resend <id>` | Rearm failed/ambiguous work for a later scan; first verify whether the external effect already happened. |
| `outbox abandon <id>` | Cancel unresolved work; preserve successful effects. |
| `ticket-check --gate <card> --task <task> --rev <N> --sha <SHA256>` | Read local state and the gate. Only exit 0 with `VALID epoch=...` permits promotion; exit 3 is invalid. |

`enabled: false` guards only `run`, not administrative commands. Pausing cron does not instantly revoke existing tickets; use the appropriate holds before maintenance affecting promotion.

**Relay shutdown is not an emergency stop or a completed rollback.** `blockers disable` cancels currently unsent relay asks/corrections but preserves queued first-answer completions and possibly-sent work for reconciliation; it queues fence releases and lets recorded parks finish only after their work card has no current run. Keep the compatible poller available to drain those effects. While the relay is off, an authorized operator handles unanswered question cards and later corrections, and must never complete a park while its worker is running. Temporary task holds pause relay handling; they do not finalize or discard questions.

Before any separately authorized code rollback, run `blockers rollback-preflight`. It refuses an enabled relay, unfinished blocker/ask outbox rows (including mirrors), releasing blockers, or open/releasing fences or parks. It does not check all unrelated work, switch releases, remove tables, revoke every ticket or restore a database. Its predicate is read-only, but the command opens the writer and may initialize missing schema. Preserve the current database; never restore an old snapshot over subsequent live effects. See [disable and rollback](docs/protocol.md#disable-and-rollback).

State lives under `integrations/clickup-dedicated-panel`: SQLite DB/WAL/SHM, lock, staging and rotating logs. Back it up consistently while stopped; never publish it. Preserve unresolved staging: it can contain exact bytes needed for safe retry. Authentication failures, status mismatch and exhausted retries need investigation, not blind resends. No automatic production deployment is included.

## Tests and verification limits

From this package directory on Windows, set the test-only executable override and run:

```powershell
$env:CLICKUP_INTAKE_TEST_HERMES_EXE = 'C:/Hermes/hermes-agent/venv/Scripts/hermes.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -v
```

The rollback compatibility case uses the supplied sanitized `tests/fixtures/rollback_base/` snapshot; it does not require access to the original private Git history. The real-CLI cases create **temporary Hermes homes/boards beneath `tests/.tmp`**; they do not start a gateway, call a model or contact ClickUp. Do not run inside a restricted live worker. They intentionally remove inherited Hermes routing variables from their own child processes to test an isolated home. Tests fail rather than pretending success if the CLI is missing or incompatible. If interrupted, verify no test process remains before removing its exact temporary home and retrying; never delete another test run's directory.

The accepted source includes isolated question/answer, correction, fence/park, disable and rollback regression coverage. Those scenarios are not observations of a live human question/answer exchange. Publication verification and the source/publish boundary are recorded in [verification notes](docs/verification.md). Offline/fake-transport and local-CLI success is **not** proof of live ClickUp UI rendering, account permissions or production readiness. The originating installation's UI quote/reply-with-quote tests were explicitly not run; do not inherit that exception as your own test PASS.
