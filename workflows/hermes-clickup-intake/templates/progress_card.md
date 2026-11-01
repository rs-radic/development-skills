# Structural progress diagnostic

Originating task: $task
Source anchor: $card
Observed run: $run_id (0 means no native run is bound here; the reason states what absence was actually verified)
Event: $event_kind at $event_created_s
Class: $class
Context fingerprint: $reason_sha256
Approval: $approval_id; revision: $rev; approved hash: $plan_sha256
Attempt: $attempt of 3. Adopted: $adopted

$reason

Run the read-only admission command immediately before diagnosis and before each action:
`$check_command`

Only CURRENT admits diagnosis. STALE means stop; UNAVAILABLE means retry observation, not permission.
Diagnostic admission is NOT approval. Null approval/revision/hash means no product execution authority.
Use only the normal, already-authorized supported same-work operation after fresh closure checks.
A logical anchor is not a native card: never show, resume, update, or ACK it. Post ACKs on this recovery carrier.
For a completed planner with invalid handoff, inspect all current parents, children and runs. Only if the
same request is still terminal and has no descendants or active owner may the authorized operator requeue
its planner through the supported update. Keep the original plan bytes and literal malformed metadata
unchanged in history. The planner must issue a fresh review request with a correct hash over unchanged
bytes; a real independent reviewer must review again. Never overwrite history or fabricate PASS.
Never reset/recreate work, replace a graph, bypass a dependency or ticket, complete a question, grant
approval, loosen validation, edit an integration, or perform an unsupported code repair from this wake.
A producer marked DONE and a completed recovery carrier are not proof of publication or resolution.

Post a first-line acknowledgement on this carrier:
`CLICKUP-RECOVERY-ACK v1 card=$card run=$run_id disposition=working`
Use disposition=cannot_act for technical inability; the coordinator retains unresolved responsibility.
For a genuinely missing human decision use disposition=owner with a second line:
`CLICKUP-RECOVERY-QUESTION v1 {"task":"$task","card":"$card","run":$run_id,"approval":$approval_id,"revision":$rev,"sha256":$plan_sha256,"fingerprint":"$reason_sha256","failed":"exact safe failure","attempted":"actual checks","decision":"exact unanswered decision","next_action":"who must do what"}`
No secrets, paths, hostnames or raw error dumps. An answer follows ordinary classification and authority
checks, not elapsed-time consent. Verify the intended continuation/publication or originating-task
explanation; queued notices and technical explanations do not constitute recovered work.
