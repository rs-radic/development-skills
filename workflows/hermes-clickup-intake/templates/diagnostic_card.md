# CLICKUP-RECOVERY diagnostic v1: inspect ${card} run ${run_id}

This unassigned carrier is completed only to wake the originating chat's main coordinating default profile. It is never dispatched. Do not mutate this carrier. ClickUp task ${task}; approval ${approval_id}, r${rev}, SHA-256 ${plan_sha256}; incident fingerprint ${reason_sha256}; event ${event_kind}@${event_created_s}; attempt ${attempt}/3.

Diagnostic admission is NOT approval, release, repair authority or proof that every failure is recoverable.

## Fresh fail-closed admission

`${check_command}`

Run this read-only check on wake, immediately before EACH operator action and again immediately before any resume. Nonzero means STOP without mutation: STALE is a delayed/duplicate/no-longer-authorized wake; UNAVAILABLE means observations are incomplete and the intake must retry. Never infer no obligations from a failed read.

## Diagnose within existing authority

Inspect the same source card's current body, comments, native parents and runs, accepted approval, exact approved plan bytes and existing recovery contracts. Distinguish an unanswered product/security/credential/approval question from missing technical bookkeeping. An approved plan that incorporates a question does not authorize you to complete it: never complete a question, fence, park or gate, bypass an approval/ticket/fence/review/E2E gate, invent a resolution, or rewrite history.

Use the supported normal lifecycle under the current approved plan/amendment and the operator-adopted recovery scope, not a fixed verb list. The operator-adopted recovery scope must be explicitly verified for this installation; admission, ownership, ordering, receipt and safety guards remain. Read back exact owned lineage/target/content, the source's explicit handoff and all current ordering preconditions before each action. Attachments still require exact approved bytes/SHA-256; a superseded replacement archive still requires named ownership and no run. Use original owners' role/tools; do not invent card bodies, graph changes, reassignment or completion, force-release a dependency, or contact ClickUp directly.

Read the operator-supplied canonical project QA policy and its `current-dev-baseline.md` procedure. For the same existing startup/baseline card, use that guarded recipe: exact plan/current-dev adoption and source handoff, idle DONE with no open run/holder/claim or healthy active descendants, existing unfinished source-parent TODO hold read back before source resume, native release only after complete handoff. Preserve owner/body/dependencies, history/evidence/counters, cleanup and normal invalidation; reuse verified remote/served SHA, DLL and database proof, with no SQL replay or unnecessary rebuild/site switch. No blanket DONE reopening, direct lifecycle SQL, private status helpers or worker-guard removal.

Two outcomes only: authorized work actually proceeds through the same native owners, or a genuinely human-required question is delivered and read back with bound reply/wait/resume routing. A cannot_act/working ACK, dispatch request, canned operator-action notice or escalated status is intermediate, not completion. If an operation/tool is unavailable, retain ownership and route the approved setup/repair to the existing capable owner; verify actual pickup/progress. Ask a human only for an actual missing decision, authority or resource unavailable within current approvals, not repeat business approval for technical bookkeeping. Never manufacture CURRENT, reset incidents or replay stale/terminal/superseded/unknown carriers.

Do not resume an unchanged refusal or invent unsupported transitions. Keep code repair with its original authorized implementation/review owner; this carrier grants no code/core/dispatcher/config/profile change or restart. A failed capability/read keeps the same recovery obligation, not a third final outcome.

## Acknowledge on the same source card

After another successful fresh admission check, post exactly one source-card ACK first line:
`CLICKUP-RECOVERY-ACK v1 card=${card} run=${run_id} disposition=working`
(or `disposition=owner` only for an actual missing decision). Explain the established failure, attempted checks and same-owner continuation or exact genuinely human-required question. Do not emit cannot_act as a final outcome; retain ownership until continuation or verified question delivery. Keep the explanation sanitized: no raw logs, paths, hosts, IPs, customer values or credentials. The integration owns originating-task delivery. Verify its readback and bound reply/wait/resume route; a delivered generic technical notice is not a human question or completion. A technical limitation routes to the existing capable owner, not new product approval.

For intermediate secret-safe technical diagnosis, the existing fixed-code projection is:
`CLICKUP-RECOVERY-DETAIL v1 failed=missing_transition attempted=inspection next=integration_repair`
Allowed failed codes: missing_transition, ticket_guard, unsupported_operation, missing_credential.
Allowed attempted codes: inspection, supported_operations, ticket_recheck. Allowed next codes: integration_repair, ticket_renewal, secure_credential. Choose only facts you actually established. Only these fixed code projections, never raw prose, may be copied for a technical limitation; their delivery alone never completes recovery.

For `disposition=owner`, the second line MUST instead be `CLICKUP-RECOVERY-QUESTION v1 ` followed by one JSON object with exactly these fields:
`{"task":"${task}","card":"${card}","run":${run_id},"approval":${approval_id},"revision":${rev},"sha256":"${plan_sha256}","fingerprint":"${reason_sha256}","failed":"specific established failure","attempted":"specific checks or supported operations actually attempted","decision":"exact missing user decision, choices and consequences","next_action":"exact user or authorized operator action needed"}`

Each of the four prose fields must be nonempty, single-line, at most 800 characters, secret-safe and suitable for the originating task. This is a bounded structured projection, not raw coordinator prose. A missing, foreign, malformed or unsafe decision is NOT delivered or completed: the integration emits a capability-failure alert and keeps the required escalation unfinished. Submit a new corrected bound ACK after rechecking current admission. Do not create/link a question card, invoke the native create/link/block question protocol or contact ClickUp yourself. The integration publishes the accepted decision on the originating task, verifies the exact comment, and records a target-bound explicit wait. A human replies in that question thread and mentions the intake bot; normal planner relay, classification and unchanged-authority resolution handle the answer. Neither the answer nor its resolution completes a native question or resumes a worker. If supported delivery or continuation is missing, route its approved repair to the existing capable owner and retain ownership; an intermediate limitation never completes the required question or recovery.

Run 0 explicitly means no worker has started behind an incorporated question; it is not an invented native run and gives no resume authority for a todo card.

For supported recovery, preserve failure history, post the existing recovery receipt with exact completed operations/readbacks, rerun admission, resume only the same source card using its supported native transition, and read it back. Never resume an unchanged technical refusal merely to repeat it. A worker behind an open question stays behind that question; diagnostic admission never authorizes question completion. Never delegate live mutations or remove a worker guard.
