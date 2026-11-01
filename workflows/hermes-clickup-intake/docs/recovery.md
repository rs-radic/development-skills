# Optional recovery and stalled-work diagnostics

Recovery wakes an operator for an exact stopped worker, a qualified stalled transition, pre-worker approval staging or structural/outer progress diagnosis within an **existing approved workflow**. It is not a generic retry engine, a new approval, or permission to bypass a worker guard. Question relay, amendment continuation and recovery have separate activation gates. Bounded attempts can remain unresolved; this is not a universal live-recovery guarantee. The [progress extension](progress-recovery.md) also distinguishes independent notification delivery failures from native worker progress, preserving one owner and the original budgets/deadline.

Keep recovery **disabled until the operator adopts a project-specific recovery contract**. The public [carrier template](../templates/recovery_card.md) requires operator-supplied references to that contract, the approved plan/amendment, the project's workflow and its supported native recovery procedure. Supply the exact board, permitted operator context, target/path/byte bindings and ordering preconditions. The preparation tool does not discover or approve that authority, substitute a private project plan, or provision a coordinator. Unresolved references are a reason not to activate recovery.

## Activation and exact-incident adoption

Prefix these commands with `<python_exe> <installed-entry>` from your prepared installation:

```text
recovery status
recovery enable
recovery adopt --card <work-card-id> --run <stopped-run-id>
recovery-check --card <work-card-id> --run <stopped-run-id>
recovery disable
recovery rollback-check
```

- `recovery enable` sets/replaces `meta.recovery_cutover_ms` with the current epoch milliseconds. It does not enable polling, the question relay, development handoff or ordinary notifications. There is no `recovery_enabled` JSON option. Repeating `enable` resets the cutoff; use `status` for inspection.
- Automatic typed-stop detection admits qualifying events at or after that cutoff, not an old backlog. Stalled/pre-worker cases use their separately qualified observations and ten-minute grace, not a fabricated native stop. An older approval can be observed after recovery activation without rewriting its history.
- `recovery adopt --card W --run R` explicitly records an existing exact incident, typically one predating activation. Recovery must already be enabled. The command reads W, requires the latest actionable typed worker stop and eligible current approval, and rejects held/terminal/ineligible tasks. It records the incident locally; it does not itself wake the operator. A subsequent enabled live scan stages/sends the wake. Repeating adoption for an already recorded `(W, R)` returns that existing record, even if finalized; it does not reopen it or prove it is still current.
- All `recovery` subcommands, **including `status` and `rollback-check`**, take the integration lock and open the writer; they can initialize a missing database/schema. They work even when polling is disabled. They do not load the ClickUp token; `adopt` additionally reads Kanban. Lock contention returns exit 1.

`main.main`, `main.recovery_adopt` and `engine._recover` implement these boundaries. Preparation creates no state database and copies no activation timestamps.

## Which stops qualify

`engine.recovery_members` binds each work card W to approval A*. Eligible approval states are `released`, `started`, `implemented`, `ticketed` and `promoted`. Membership includes the current orchestrator and its declared final-E2E, promotion, cleanup and lineage cards. It also includes the **last** prior `clickup_gates` lineage handed to that approval's own completed orchestrator-create row. This is not permission to scan arbitrary board cards or all historical ancestors. Every approval gate belonging to the task is excluded, even if named in lineage.

With the current native observation contract, `dependency_scope`/`dependency_members` additionally derive uniquely owned required ancestors of current approved roots. This reaches later-added repair work and its question without rewriting declared approval membership. Complete reciprocal graph evidence, current generation/approval and foreign/shared ownership guards apply again at effect time and public admission. A missing read is unavailable, not affirmative detachment. Visibility is not new authority to operate on that card. See [current required dependencies](progress-recovery.md#current-dependency-observation-is-not-new-authority).

The task must be unheld and nonterminal, with current classified input and approval, in `development`, `implemented`, `promotion_window`, `promotion_held` or `ready_for_deployment`. Evaluation requires a confirmed task observation. Missing reads defer a decision; they do not prove a stop has resolved.

`kanban_port.recovery_incident` then requires:

- W is `blocked` with a newest `blocked` event, or `triage` with a newest `block_loop_detected` event.
- That event belongs to W's latest run R; R was genuinely claimed and ended with outcome `blocked`.
- The typed reason is `capability`, `transient` or `needs_input`. Dependency waits, initially blocked/unclaimed cards and ordinary failures are not automatically retried.
- A `needs_input` reason beginning `ClickUp approval ticket INVALID` stays on the approval/owner path, not recovery.

The typed-stop identity is `(W, R)`, plus event kind/time, block kind, SHA-256 of the full reason, approval ID/revision and plan SHA-256. Ordinary request/planning cards do not qualify merely because they are on the same board; membership under A* is required. Existing incidents remain monitored across supported handoffs and later claims without refunding their original budgets; a claim alone is not proof of recovery.

### Stalled transitions and pre-worker staging

`stalled.candidate` additionally recognizes fully observed stopped `blocked`/`triage` work and narrow `todo` cases behind an incorporated material question or a qualifying stopped internal question-delivery transition. It checks recursive native parents, question/fence obligations and reviewed source bytes. The undelivered-question exception is specific to that question's status, never its ancestry or live runs. Normal ready/todo queues, verified unanswered human dependencies and unknown reads do not qualify through this specialist. The separate outer diagnostic can investigate unchanged automation/observation failure without granting source execution. The no-worker case uses run **0** anchored to actual creation, not an invented claimed run or block event. Ten minutes of unchanged qualifying observations is a diagnostic grace, not a start-time SLA.

`preworker.inspect`/`detect` handles a current reviewed/approved plan whose gate/orchestrator staging has made no verified progress for ten minutes, including no native objects yet or partial creation/attachment/registration. `approval-<approval-id>` is a **logical anchor**, not a Kanban card; run 0 means no native incident run. The current-review PASS and complete actual ancestor/target obligations must be observed. Live writers, future retries, ordinary progress, disabled handoff, holds and changed authority prevent/defer admission.

The normal intake path alone may reconcile/create/register/attach/release those exact staged objects. Preserve returned-but-unregistered IDs and their idempotency keys. Never manually resume an approval anchor, create a replacement graph or reset state/history. `main.Glue.early_progress` prioritizes current staging before unrelated historical collection, but final effect-time admission still applies.

Superseded blocker audit rows no longer veto current staging merely by existing. Current native obligations, approval/input authority and human-question readbacks still govern. Structural `progress_stall` carriers may bind an actual source or a logical `progress-…` anchor, with no product approval tuple where none exists. Their ACKs go on the actual carrier. Missing native evidence permits truthful observation-failure diagnosis only. The [progress procedure](progress-recovery.md) governs the outer grace, safe planner re-review path, current admission and temporary delivery-owner retirement; do not use the typed-stop source-operation procedure on a logical anchor.

## Carrier, wake and admission

`engine._attempt` queues an ordered chain on a **new intake-owned carrier**, never on W: create it unassigned and blocked, subscribe it with `delivery_mode="wake"`, then complete it. No model runs inside the poller. The carrier is not dispatched as a worker; its completion wakes the configured project chat's coordinator. Ordinary card progress subscriptions remain passive `notify`. Recovery wakes are independent of the ordinary `notify_mode` option.

The carrier body holds the full binding and a clipped blocked reason. `render.recovery_card_body` caps the reason at 4,000 characters and the whole body at 16 KiB of UTF-8, without shortening the fixed procedure/binding. `render.recovery_summary` caps the wake summary at 400 characters. These limits do not make private data safe: carrier content stays on the operator's internal board/chat and must remain private.

Immediately before completing a carrier, `main.Glue.recovery_gate` performs fresh incident/authority admission. Unavailable reads or holds defer; stale incidents and superseded attempts retire without a fresh wake. An owner ACK must follow the bound question path. A technical `cannot_act` is intermediate, not a completed recovery or a universal cancellation of later attempts. Possibly-sent effects retain normal outbox readback/reconciliation. An already delivered wake cannot be recalled.

### `recovery-check` is read-only, not authorization

The coordinator must execute the rendered command on wake, immediately before **each** board mutation, and again immediately before resuming W:

```text
<python_exe> <installed-entry> recovery-check --card <work-card-id> --run <stopped-run-id>
```

| Result | Required response |
|---|---|
| Exit 0, `CURRENT incident=...` | The recorded incident still passes the admission predicate. Independently verify the approved authority and operation preconditions; this result does not grant them. |
| Exit 3, `STALE <reason>` | No board write or ACK. Reply once in the operator chat that this stale wake requires no action, then stop. |
| Exit 2, `UNAVAILABLE <reason>` | Stop without an ACK or board mutation. Later polling can retry or escalate. |

`main.recovery_check` uses `store.load_recovery_snapshot`/`open_readonly`. Current native checks validate the approval-bound dependency snapshot; diagnostic checks additionally read obligations/approved attachments and re-read local state. Pre-worker/progress checks use the native snapshot described below, not a fake `show` for a logical anchor. A delivery-only verdict explicitly carries `scope=delivery-only`; it is not permission to resume source work. The command takes no intake lock, opens no writer, loads no ClickUp credential and creates no missing state database. It can run while the writer lock is held. SQLite may leave sidecars; read-only does not mean no filesystem artifacts.

The verdict rejects stale identity/authority, held/terminal/wrong-phase tasks and ineligible current state; unknown evidence remains unavailable. Exact predicates differ by incident class. `CURRENT` is not an ACK, operator permission or proof that a capable owner is running. Question publication/wait state is rechecked at the send/admission boundary; neither a technical inability nor a previously answered question permits a stale restart.

### Native pre-worker snapshot compatibility

`KanbanCli._preworker_snapshot` imports the installed board resolver/read contract using the configured executable's standard `<root>/venv/Scripts/hermes.exe` layout. It does not call an invented snapshot CLI. It refuses a custom child environment, in-progress update or changed native read-contract files. CLI availability alone does not prove compatibility with the native schema, JSON/scalar/NULL fields or resolver.

`preworker_snapshot.acquire` opens the resolved native database with `mode=ro` and captures the required ancestor closure in one SQLite read transaction, including a completed orchestrator's promotion target. Approved attachment bytes/hashes are checked separately. The same connection's `data_version`, file identity and native-source stability invalidate stale proof; this is not a cross-connection revision token. Acquisition over five minutes fails; proof use is bounded to ten minutes. The target shares a fixed 64-read allowance across collection and required refresh work, counting failed attempts. Persisted discovery hints are not effect-time evidence. Unknown/changed obligations, missing bytes or expired observations fail closed.

## What an operator may do

Only the project's explicitly authorized, unfenced **non-worker operator context** may act. A dispatcher worker or delegated child is not that context. Read W's body, comments, stopped-run summary, named handoffs and exact approved attachments before classifying:

1. **Technical continuation:** the existing plan/amendment already authorizes the operation.
2. **Owner-only:** a product choice, new scope/authority, approval/ticket issue or security/containment waiver is required.
3. **Cannot act yet:** a precondition fails or the operator lacks an already-authorized capability/credential. This is an intermediate limitation, not successful recovery or automatically a human decision.

For technical continuation, every operation requires the exact source handoff, owned target/content and current ordering readbacks. The current protocol uses **operator-adopted recovery scope**, not the superseded universal archive/attach/resume-only whitelist. This changes no authority by itself. Examples retaining their explicit preconditions are:

- **Archive** a card W owns under A*, explicitly named for archive/replacement, with no run as required by the carrier contract.
- **Attach** exact approved bytes to a card W owns under A*, after verifying their SHA-256.
- **Resume W** only through a verified, supported native transition for its actual current state and adopted authority, with satisfied obligations. This package does not supply a universal dashboard transition or authorize an improvised state change.

The typed-stop protocol additionally permits the specified receipt/ACK comments **on W**. It does not grant arbitrary comments or edits elsewhere. Before resumption, post a `CLICKUP-RECOVERY-RECEIPT v1 card=W run=R` first line with operations and readbacks, worker-owned operations returned to W, and any deferred operation's exact precondition/re-request. Recheck admission, resume, then read W back. Never resume merely to repeat an operation that neither actor can perform. Pre-worker anchors and structural progress diagnostics instead use their actual carrier for ACKs; never write to a logical anchor or mistake delivery-only admission for a source-operation receipt.

Worker-owned create/link/comment/own-card attachment work returns to W. Deferred operator work waits for its required ordering; W re-requests it with a new typed `CLICKUP-RECOVERY-REQUEST` capability stop after satisfying the named precondition. Where the adopted contract requires an assembly hold for a new card awaiting a deferred input, W creates it with only W as parent. It must stay `todo`, with no runs or ready/claimed event, until W verifies its inputs and completes. The operator neither releases that child nor fabricates its dependencies. See the complete [carrier procedure](../templates/recovery_card.md).

**Never** invent a graph, reassignment or completion as a recovery shortcut; complete question/fence/park/approval-gate cards; contact ClickUp directly; complete W for its worker; edit lifecycle SQL; remove delegated-child or `HERMES_KANBAN_*` guards; delegate live mutations; or infer gateway/core/config/profile/credential/schedule changes from this carrier. Separately authorized repair stays with its original implementation/review owner. Recovery grants no blanket permission, new approval, security exception, release-branch merge or production deployment.

## ACKs, budget and actual resumption

For a genuine owner-only decision, do not resume. Default posts the exact first line on W (on the actual carrier for a pre-worker anchor or structural progress diagnostic):

```text
CLICKUP-RECOVERY-ACK v1 card=<work-card-id> run=<stopped-run-id> disposition=owner
```

Diagnostics require a second `CLICKUP-RECOVERY-QUESTION v1` line with the JSON binding in the carrier template: task, card/run, approval/revision/hash, fingerprint and `failed`, `attempted`, `decision`, `next_action`. Each prose field must be nonempty, single-line and at most 800 characters; the whole line is bounded to 6,000. `stalled.diagnostic_ack` recognizes default's decisions; the named coordinator's `cannot_act` is only technical inability. A retained legacy typed-stop prose path is narrow and never accepts a malformed structured question by downgrading it to prose. Invalid/foreign/unsafe questions remain unfinished and raise a capability alert.

The general content filter is retired, but these projections retain explicit `sk-`, path/IP/non-ClickUp-URL checks. Fixed allowlisted technical-detail codes may be projected; raw ACK prose/logs are not a generic publication channel. Workers must still keep all secrets private.

An ACK or a canned technical notice is **not a verified human wait**. The intake publishes a bound question, verifies the exact provider comment and stores its root/reply binding. A human replies in that question thread and mentions the bot; normal planner relay/classification and an unchanged-authority resolution process the answer. A local `publishing` flag, queue entry or native `blocked` label is only automation/delivery state. Missing delivery, cancelled/superseded notices and unavailable continuation remain unfinished. Neither a diagnostic answer nor its resolution completes a native question or resumes work.

`engine._recovery_step` enforces:

- At most **three wake attempts per incident**, first on detection, then due at detection +60 and +180 minutes; unresolved work normally escalates at +360 minutes.
- A working ACK defers further wake/escalation to at least three hours after the latest valid working ACK, but never past the incident's **detection +24-hour ceiling**. This is not a rolling three-wakes-per-day allowance. Missed scans do not remove that ceiling.
- After three recorded incidents for the same `(task, W, A*)`, a fourth escalates without a wake; this recurrence count has no daily reset.
- A supported ready/review handoff is tracked rather than mistaken for recovery; its three-hour pickup grace is bounded by the original deadline. No fresh budget is created by changing the claim, context or notice payload.
- A later claimed run requires matching live process-generation and fresh-heartbeat evidence before it counts as capable-owner progress. `done`/`archived` is tracked as closed; neither it nor an assignment retroactively proves recovery execution. Changed authority can supersede an incident. Holds pause evaluation; no wall-clock response SLA is promised.

Escalation queues an originating-task result, **not successful delivery**. Typed/stalled incidents may ask which existing authorized operator can take over or restore an unavailable recovery resource after bounded automation is exhausted; current binding, exact readback and usable reply routing still apply. Pre-worker staging does not manufacture this fallback: without actual progress or a valid coordinator question it can remain open with exhausted attempts. A technical `cannot_act`/`working` ACK, canned notice or dead claim is intermediate, not a third successful outcome. Preserve the original failure, obligation, deadlines and spent budgets. Successful recovery means actual authorized capable-owner progress or a verified genuinely human-required question with its bound reply path.

## Disable, drain and rollback

`recovery disable` removes only the recovery cutoff, marks open/handed-off incidents disabled, cancels unsent attempt effects, and queues silent archival of created carriers whose wake was not completed. It does not disable the human-decision relay, ordinary intake/notifications, stop workers, undo operator actions or retract delivered wakes. Keep the compatible poller available to reconcile possibly-sent effects and drain queued archival. A delayed wake must still run admission and stop as stale.

`recovery rollback-check` exits 0 with `recovery rollback-check OK` only when recovery is off, all recovery `:rcv:` outbox rows are `done` or `cancelled_stale`, and no incident is `open` or `handed_off`. Otherwise it exits 1 with reasons. Its predicate is recovery-only; the command's writer setup can initialize schema. It does not drain anything, switch a release, restore a database, or approve a downgrade that removes question-relay support. If the target also predates that relay, its separate `blockers rollback-preflight` gate still applies.

Recovery/amendment tables and metadata are additive under schema version 1. The bundled recovery-baseline tests exercise an older reader only after qualifying work is finalized; they do not authorize restoring an old database over subsequent live effects. Separate question-relay and [amendment/admission](amendments.md) rollback checks remain necessary. Preserve state and deployment bindings, and use a separately approved upgrade/rollback procedure. See [verification](verification.md) for current evidence versus historical tests; no native UI or live recovery success is implied.
