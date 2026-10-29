# CLICKUP-RECOVERY approval staging v1

This unassigned carrier wakes the original coordinating default profile. It is not dispatched. Target `${card}` is a logical approval anchor, not a Kanban ID; run ${run_id} means no native incident run. No native card or run is implied. Task ${task}; approval ${approval_id}; r${rev}; exact plan SHA-256 ${plan_sha256}; fingerprint ${reason_sha256}; approval event ${event_created_s}; attempt ${attempt}/3.

`${check_command}`

Run this read-only admission check on wake and before every action. Nonzero means STOP: delayed/duplicate/stale approval or unavailable observation is never authority. Inspect the exact approval, stage outbox and existing native objects by their actual IDs. A completed gate-create result may exist before registration. Preserve that result and its idempotency key; do not make a duplicate gate or graph.

Use the normal intake poll and its existing guarded stage/release path. The poll owns creating/reconciling the exact gate and orchestrator, attaching reviewed plan/approval/current context, registering them, and releasing the orchestrator. Do not perform those operations manually, alter the database, reset approvals/budgets, forge worker history, or directly resume an approval anchor. Do not change core, configuration, schedules, credentials, permissions or product scope. Actual native work remains with the original owners and existing admission/fence/review controls. A writer with a live process or a due retry is not a missing worker.

${reason}

There are only two successful outcomes: actual authorized capable-owner progress through the existing native lifecycle, or an exact genuinely human-only question delivered and read back on the original task with a bound reply and normal classification/continuation. A wake, assignment, working/cannot_act ACK, canned operator notice or queued question is not success. Keep the same obligation, original deadlines and spent attempts. Escalating a technical limitation does not authorize a new planning graph or repeat development approval.

For a genuine missing decision, authority or resource only: after a fresh CURRENT check, post on THIS actual carrier (not `${card}`) as default:
`CLICKUP-RECOVERY-ACK v1 card=${card} run=0 disposition=owner`
Second line: `CLICKUP-RECOVERY-QUESTION v1 ` followed by exactly:
`{"task":"${task}","card":"${card}","run":0,"approval":${approval_id},"revision":${rev},"sha256":"${plan_sha256}","fingerprint":"${reason_sha256}","failed":"specific established failure","attempted":"specific checks actually attempted","decision":"exact genuinely human-only decision, choices and consequences","next_action":"exact action needed without credentials"}`

Each prose field must be single-line, nonempty, at most 800 characters and secret-safe. No raw logs, paths, hosts, IPs, customer data or credentials. No fallback to an unbound or malformed question. The intake owns publication, exact readback, targeted thread reply, normal planner classification and unchanged-authority resolution. Do not contact ClickUp directly or create/complete a native question, fence, gate or park. After the accepted answer, only the same normally guarded intake stage path may continue. Neither an answer nor admission is permission to bypass any gate. If tooling is unavailable, retain ownership and use only existing authorized capability; no fabricated successful outcome.
