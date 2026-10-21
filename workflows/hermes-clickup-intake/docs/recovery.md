# Optional technical-blocker recovery

Technical recovery wakes an operator to inspect a stopped worker within an **existing approved workflow**. It is not a generic retry engine, a new approval, or permission to bypass a worker guard. It is separate from the human-decision question relay: either feature can be disabled while the other remains enabled.

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
- Automatic detection admits qualifying stop events at or after that cutoff, not an old backlog. The stopped run's blocking-event time governs, not its card's creation time.
- `recovery adopt --card W --run R` explicitly records an existing exact incident, typically one predating activation. Recovery must already be enabled. The command reads W, requires the latest actionable typed worker stop and eligible current approval, and rejects held/terminal/ineligible tasks. It records the incident locally; it does not itself wake the operator. A subsequent enabled live scan stages/sends the wake. Repeating adoption for an already recorded `(W, R)` returns that existing record, even if finalized; it does not reopen it or prove it is still current.
- All `recovery` subcommands, **including `status` and `rollback-check`**, take the integration lock and open the writer; they can initialize a missing database/schema. They work even when polling is disabled. They do not load the ClickUp token; `adopt` additionally reads Kanban. Lock contention returns exit 1.

`main.main`, `main.recovery_adopt` and `engine._recover` implement these boundaries. Preparation creates no state database and copies no activation timestamps.

## Which stops qualify

`engine.recovery_members` binds each work card W to approval A*. Eligible approval states are `released`, `started`, `implemented`, `ticketed` and `promoted`. Membership includes the current orchestrator and its declared final-E2E, promotion, cleanup and lineage cards. It also includes the **last** prior `clickup_gates` lineage handed to that approval's own completed orchestrator-create row. This is not permission to scan arbitrary board cards or all historical ancestors. Every approval gate belonging to the task is excluded, even if named in lineage.

The task must be unheld and nonterminal, with current classified input and approval, in `development`, `implemented`, `promotion_window`, `promotion_held` or `ready_for_deployment`. Evaluation requires a confirmed task observation. Missing reads defer a decision; they do not prove a stop has resolved.

`kanban_port.recovery_incident` then requires:

- W is `blocked` with a newest `blocked` event, or `triage` with a newest `block_loop_detected` event.
- That event belongs to W's latest run R; R was genuinely claimed and ended with outcome `blocked`.
- The typed reason is `capability`, `transient` or `needs_input`. Dependency waits, initially blocked/unclaimed cards and ordinary failures are not automatically retried.
- A `needs_input` reason beginning `ClickUp approval ticket INVALID` stays on the approval/owner path, not recovery.

The durable identity is `(W, R)`, plus event kind/time, block kind, SHA-256 of the full reason, approval ID/revision and plan SHA-256. A later claimed run is a different incident. Ordinary request/planning cards do not qualify merely because they are on the same board; membership under A* is required.

## Carrier, wake and admission

`engine._attempt` queues an ordered chain on a **new intake-owned carrier**, never on W: create it unassigned and blocked, subscribe it with `delivery_mode="wake"`, then complete it. No model runs inside the poller. The carrier is not dispatched as a worker; its completion wakes the configured project chat's coordinator. Ordinary card progress subscriptions remain passive `notify`. Recovery wakes are independent of the ordinary `notify_mode` option.

The carrier body holds the full binding and a clipped blocked reason. `render.recovery_card_body` caps the reason at 4,000 characters and the whole body at 16 KiB of UTF-8, without shortening the fixed procedure/binding. `render.recovery_summary` caps the wake summary at 400 characters. These limits do not make private data safe: carrier content stays on the operator's internal board/chat and must remain private.

Immediately before completing a wake carrier, `main.Glue.recovery_gate` reads W afresh and applies `kanban_port.recovery_verdict` to the current state. An unavailable read or held task defers the send. A stale incident or final `owner`/`cannot_act` ACK cancels the wake and queues silent carrier archival. Unsent older attempts are retired; possibly-sent effects retain normal outbox readback/reconciliation. An already delivered chat wake cannot be recalled.

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

`main.recovery_check` uses `store.load_recovery_snapshot`/`open_readonly` and one fresh Kanban show. It takes no intake lock, opens no writer, loads no ClickUp credential and creates no missing state database. It can run while the writer lock is held. SQLite may leave empty WAL sidecars; read-only does not mean no filesystem artifacts at all.

The verdict rejects disabled/finalized incidents, held/terminal/wrong-phase tasks, changed/revoked/superseded approvals, nonmembers, changed status/event/reason, newer runs and still-open workers. ACK finality is also checked by the **wake send gate** and later evaluator; `recovery-check` itself is not an ACK parser. A known final `owner`/`cannot_act` disposition must not be treated as permission to restart action just because another check still says `CURRENT` before the next evaluation.

## What an operator may do

Only the project's explicitly authorized, unfenced **non-worker operator context** may act. A dispatcher worker or delegated child is not that context. Read W's body, comments, stopped-run summary, named handoffs and exact approved attachments before classifying:

1. **Technical continuation:** the existing plan/amendment already authorizes the operation.
2. **Owner-only:** a product choice, new scope/authority, approval/ticket issue or security/containment waiver is required.
3. **Cannot act:** the operation is outside the permitted set, a precondition fails, or the operator lacks the capability/credential.

For technical continuation, every operator operation must have its exact target named by W's handoff, its content fixed by approved authority, and its current ordering preconditions read back. The permitted operator-only operations are exactly:

- **Archive** a card W owns under A*, explicitly named for archive/replacement, with no run as required by the carrier contract.
- **Attach** exact approved bytes to a card W owns under A*, after verifying their SHA-256.
- **Resume W** using the verified native transition appropriate to its current state: native `unblock` for `blocked`, or the adopted native dashboard `ready` transition for a `triage` loop whose recorded `source_status` is not `review`, with satisfied parents. Other states require refusal, not an improvised transition.

The protocol additionally permits the specified receipt/ACK comments **on W**. It does not grant arbitrary comments or edits elsewhere. Before resumption, post a `CLICKUP-RECOVERY-RECEIPT v1 card=W run=R` first line with operations and readbacks, worker-owned operations returned to W, and any deferred operation's exact precondition/re-request. Recheck admission, resume, then read W back. Never resume merely to repeat an operation that neither actor can perform.

Worker-owned create/link/comment/own-card attachment work returns to W. Deferred operator work waits for its required ordering; W re-requests it with a new typed `CLICKUP-RECOVERY-REQUEST` capability stop after satisfying the named precondition. Where the adopted contract requires an assembly hold for a new card awaiting a deferred input, W creates it with only W as parent. It must stay `todo`, with no runs or ready/claimed event, until W verifies its inputs and completes. The operator neither releases that child nor fabricates its dependencies. See the complete [carrier procedure](../templates/recovery_card.md).

**Never** create/link/unlink/reassign/complete cards or edit their bodies as an operator workaround; mutate question/fence/park/approval-gate cards; contact ClickUp from the worker/coordinator; complete W for its worker; edit lifecycle SQL; remove delegated-child or `HERMES_KANBAN_*` guards; delegate the mutation; restart the gateway; or change profiles, subscriptions or configuration. Recovery grants no blanket permission, new approval, security exception, release-branch merge or production deployment.

## ACKs, budget and actual resumption

For owner-only/cannot-act outcomes, do not resume. Post the exact first line on W and then the specific question or gap:

```text
CLICKUP-RECOVERY-ACK v1 card=<work-card-id> run=<stopped-run-id> disposition=owner
```

Use `disposition=cannot_act` for an inability to execute. If authorized work will take longer than **one hour**, first post `disposition=working`. `kanban_port.recovery_ack` accepts only an exact whole first line, a non-intake author and a timestamp at/after the stop. The earliest valid `owner` or `cannot_act` ACK is final for that incident; later working ACKs cannot override it.

`engine._recovery_step` enforces:

- At most **three wake attempts per incident**, first on detection, then due at detection +60 and +180 minutes; unresolved work normally escalates at +360 minutes.
- A working ACK defers further wake/escalation to at least three hours after the latest valid working ACK, but never past the incident's **detection +24-hour ceiling**. This is not a rolling three-wakes-per-day allowance. Missed scans do not remove that ceiling.
- After three recorded incidents for the same `(task, W, A*)`, a fourth escalates without a wake; this recurrence count has no daily reset.
- A native handoff to `ready`/`review` stops further wakes and is tracked as `handed_off`; if no worker starts within three hours of the handoff, the incident escalates. This separate handoff grace is not evidence of execution.
- A later genuinely claimed run means `resumed`; `done`/`archived` means `closed`; a native release that remains `todo` behind parents means `gated`. Changed authority can supersede an incident. Holds pause evaluation; no wall-clock response SLA is promised.

An escalation emits one durable operator alert and finalizes that incident. A coordinator reply or receipt is not proof of a resumed worker. Preserve the stopped run, its failure, evidence and recorded budgets.

## Disable, drain and rollback

`recovery disable` removes only the recovery cutoff, marks open/handed-off incidents disabled, cancels unsent attempt effects, and queues silent archival of created carriers whose wake was not completed. It does not disable the human-decision relay, ordinary intake/notifications, stop workers, undo operator actions or retract delivered wakes. Keep the compatible poller available to reconcile possibly-sent effects and drain queued archival. A delayed wake must still run admission and stop as stale.

`recovery rollback-check` exits 0 with `recovery rollback-check OK` only when recovery is off, all recovery `:rcv:` outbox rows are `done` or `cancelled_stale`, and no incident is `open` or `handed_off`. Otherwise it exits 1 with reasons. Its predicate is recovery-only; the command's writer setup can initialize schema. It does not drain anything, switch a release, restore a database, or approve a downgrade that removes question-relay support. If the target also predates that relay, its separate `blockers rollback-preflight` gate still applies.

The `recoveries` table is additive under schema version 1. The bundled recovery-baseline tests exercise an older compatible reader only after recovery work is finalized, including a case with the question relay still enabled. They do not authorize restoring an old database over subsequent live effects. Preserve state and deployment bindings, and use a separately approved upgrade/rollback procedure.
