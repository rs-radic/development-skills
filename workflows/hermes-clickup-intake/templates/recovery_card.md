# CLICKUP-RECOVERY v1: act as operator for ${card} run ${run_id}

Owner of this card: clickup-intake (deterministic, no LLM). This carrier card only wakes the configured board's main-chat coordinator: the board's main-chat operator (default profile, an unfenced non-worker context with the supported `hermes kanban` CLI). It has no assignee and is never dispatched. Do not complete, archive, unblock or comment on it.

## Binding

- ClickUp task ${task}; watched card W = ${card}; stopped run R = ${run_id}
- Event E: `${event_kind}` created at ${event_created_s} (epoch seconds); block kind `${block_kind}`; class `${class}`; source_status `${source_status}`
- SHA-256 of the full blocked reason: ${reason_sha256}
- Approval A*: id ${approval_id}, plan r${rev}, plan SHA-256 ${plan_sha256}
- Wake attempt ${attempt} of 3; adopted ${adopted}

## Blocked reason of run ${run_id} (clipped to 4,000 characters and to the 16 KB UTF-8 card-body bound)

${reason}

## 1. Fail-closed admission (before anything else)

`${check_command}`

It exits 0 and prints `CURRENT incident=...` only for the exact open incident. Run it (a) now, on wake, before you use anything else to decide; (b) again immediately before each board mutation; (c) once more immediately before you resume W. Any non-zero result stops you with no further board write:

- exit 3 `STALE <reason>` (resolved, superseded, revoked, held, terminal, re-claimed or changed W, final incident): reply once in this chat `CLICKUP-RECOVERY stale wake ${card} run ${run_id}: <reason>; no action` and stop. Post nothing on the board. This is the delayed or duplicate wake path.
- exit 2 `UNAVAILABLE <reason>`: stop without an ACK. The intake re-wakes on its schedule and escalates if needed.

The check is fail-closed only: it can stop you, it never authorizes anything that section 3 does not.

## 2. Read

- `hermes kanban --board <configured-board> show ${card} --json`: body, comments, the run ${run_id} summary, referenced handoff files and the approved plan attachments.
- The operator-supplied, approved project workflow and its operator recovery procedure. Locate these through the installation's own policy references, not another project's or worker profile's private paths.
- The project's approved plan and exact recovery handoff, including any required attachment paths, sizes and SHA-256 hashes. Verify the bytes before relying on them. See `docs/recovery.md` in this package for the required contract.

Replace `<configured-board>` in commands with the board name from this installation. Your authority is approval A* plus an explicitly adopted owner recovery rule. This package grants neither. If either is absent or the exact operation is not already authorized, classify (B) or (C); do not mutate the board.

## 3. Classify and own operations

- (A) Technical continuation within A*: the worker's requested operations are already authorized by the approved plan or amendment, or by the orchestrator card's approved routing.
- (B) Owner-only: a new product choice, scope expansion, ungranted authority, approval/ticket problem, or a security or containment waiver.
- (C) Cannot act: the needed operation is outside the permitted set below, a precondition fails, or this context also lacks the capability or credential.

For (A) you perform only operator-only operations (verbs absent from a dispatcher worker's tools), and each only when all hold: W's handoff (block reason, comment or referenced handoff file) names the operation and its exact target; A* authorizes it; its exact content is fixed by that authority (an attachment: the exact approved bytes with SHA-256 verified; an archive: a card that W owns under A*, that the authority names for archive or replacement, and that has no run); every ordering precondition stated by the handoff or plan holds now on the board and has been read back. The permitted operator-only verbs are exactly: `archive` of such a card; `attach` of exact approved bytes to a card W owns under A*; the state-appropriate resume of W (section 4).

You never create, link, unlink, reassign, complete or edit the body of any card. You never touch CLICKUP-QUESTION, CLICKUP-FENCE, CLICKUP-PARK or gate cards, never contact ClickUp, and never approve or waive anything.

Everything else returns to W:

- Operations within W's own tools (create, link, comment, own-card attach): W performs them after it resumes.
- Deferred operator-only operations, whose precondition W's own work will satisfy: list them in the receipt. W does its part, then blocks again with exactly `kanban_block(kind="capability", reason="CLICKUP-RECOVERY-REQUEST <operations and exact targets>; <now-satisfied precondition>")`. That block is a new bound incident and a new wake.
- Operator-only operations whose content the authority does not fix (for example an unspecified body edit): you do not author them. Existing-card amendments use W's comment route with exact path/size/SHA references to approved attachments; if W still needs an operator write it requests it in a new capability block with the exact content.
- Assembly hold for a new card that consumes a deferred operator input: when W must create a card that would be dispatchable at once (no unfinished parent) and whose required input or cleanup/journal target is supplied only by a deferred operator-only operation, W creates it with `parents=["<W>"]`, here `parents=["${card}"]`, and no other parent. Native creation lands it in `todo` while W is not done, so the dispatcher cannot claim it; W's own `kanban_complete`, after W has read back those inputs, is its only release. `initial_status=blocked` is never used for this (a worker cannot unblock it and you have no unblock verb for it). You never release, unblock or link that card. In a later stage verify the hold before attaching or archiving anything: status `todo`, parents exactly [W], no runs, no `ready` or `claimed` event. A card that is unheld or was already dispatched is (C) with the exact gap, and you make no mutation.
- Staged handoffs: perform an operator-only operation only once its stated ordering precondition holds now (for example a replacement promotion linked to cleanup before the superseded promotion is archived). Until then it is deferred and the receipt states the exact re-request and W's completion condition (the readbacks W must make before `kanban_complete`).

If nothing is executable now, nothing can be returned to W and nothing can be deferred, the incident is (C).

## 4. Receipt and native resume of W

After the operator-only operations and their readbacks:

1. Post one receipt comment on W whose first line is exactly `CLICKUP-RECOVERY-RECEIPT v1 card=${card} run=${run_id}`, then list the operations done with readbacks, the operations returned to W, and any deferred operation with its precondition and the exact re-request W must make.
2. Run the admission check (1c).
3. Resume W with the native transition for its state:
   - `blocked`: `hermes kanban --board <configured-board> unblock ${card}` (restores its recorded lane, or `todo` while parents remain open).
   - `triage` through `block_loop_detected` whose payload `source_status` is not `review`: the installed Kanban dashboard handler `update_task("${card}", UpdateTaskBody(status="ready"), board="<configured-board>")` (worker-startup-recovery.md, "Continue an authorized saved checkpoint", step 5); it refuses unless W's parents are satisfied.
   - any other state: classify (C) and make no transition.
4. Read back W.

Never resume W before the executable operator-only operations are done, and never when its only remaining need is an operation you cannot perform: that would only repeat the refused call.

## 5. Owner-only or cannot act

For (B) or (C) do not decide and do not resume W. Post on W a comment whose first line is exactly `CLICKUP-RECOVERY-ACK v1 card=${card} run=${run_id} disposition=owner` (or `disposition=cannot_act`), followed by the precise question or gap. For (B) ask the owner one precise question in this channel. If (A) work will take longer than one hour, first post `CLICKUP-RECOVERY-ACK v1 card=${card} run=${run_id} disposition=working` to defer re-wakes.

## Never

- edit lifecycle SQL, or remove or strip the delegated-child-context marker, `HERMES_KANBAN_*` variables or any other worker guard;
- restart the gateway, or change profiles, subscriptions or config;
- use `delegate_task` or any worker for these mutations (those contexts are fenced);
- complete W on the worker's behalf.

Preserve run ${run_id}, its failure and all evidence. Success is W's resumed worker (a new run) or its remaining dependency gate, observed by the intake; your reply is not the success criterion.
