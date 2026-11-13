# Assigned runtime recovery — ${task}, ${card}, stopped run ${run_id}

Assignee: integrator. This is an actual dispatcher-owned recovery task, not a completed wake carrier. Prerequisite: the operator must explicitly authorize this installation’s bounded answer-continuation and assigned-task recovery fallback before enabling it. This public template supplies no standing project authority. This task confers no extra worker permissions. Preserve the source owner, implementation/review/test/release/cleanup graph and all gates. Do not create replacement recovery tasks.

## Exact binding and unresolved obligation

- Originating ClickUp task: ${task}; source card/anchor: ${card}; source run: ${run_id}
- Event: ${event_kind}@${event_created_s}; class ${class}; block kind ${block_kind}; source status ${source_status}
- Reason fingerprint: ${reason_sha256}
- Existing authority: approval ${approval_id}, revision ${rev}, plan SHA-256 ${plan_sha256}
- Historical wake attempts: ${attempt}; adopted ${adopted}. Never reset counters or deadlines.

${reason}${answer}

## Read, diagnose and act within existing authority

Run `${check_command}` now and immediately before each mutation. CURRENT is admission only, not permission. STALE: stop; preserve the exact refusal and route it to the main operator. UNAVAILABLE: record the concrete unavailable observation, not an invented product decision.

Read the source card/anchor, its current runs, comments, dependencies and authoritative attachments. Re-read the original approval and answer. If this is a logical workflow anchor (class progress_stall or approval_staging), diagnose its referenced native graph; never try to mutate the logical anchor. Confirm the required next action and current capable owner. Inspect existing integration/recovery evidence rather than replaying history.

Perform only diagnosis and supported reconciliation that your current native worker tools and the original approval permit. For cross-card transitions or any other worker-fenced operation, make a precise operator handoff to the actual default main-chat operator in the configured project channel through this task's existing notify+wake subscription. On THIS task, use kanban_comment with source/run, incident/answer/relay, exact target IDs and approved content hashes, current readbacks, executable operations in order, their authority and preconditions, and the observable result required. Then kanban_block(kind="capability", reason="CLICKUP-RECOVERY-REQUEST <exact supported operations/targets>; <verified satisfied preconditions and comment reference>"). The operator must verify CURRENT and current readbacks before acting, then return the SAME task through supported lifecycle. Never remove HERMES_KANBAN_* markers, impersonate default, use raw lifecycle SQL, private status helpers or change Hermes/core/profiles/config/schedules/credentials.

## Completion and failures

Creation, a live claim, a receipt, a sent notification or source unblock alone is not success. Keep this task unfinished until the original owner's required work actually progresses, verified by fresh matching source run/process and required prerequisite work or a genuine evidenced blocker is recorded and routed. Record proof on this task. Use the normal heartbeat while working. Genuine provider/capability/resource failures stay on this SAME task via the typed native block and its operator subscription; crashes and failed claims use the ordinary dispatcher lifecycle. Never manufacture a hold because time passed. A human question must name a real missing decision/authority/resource and use the original supported question owner/route, not direct ClickUp writes or repeated approval. Never mark the original incident recovered yourself.

If the original work progresses, record exact proof and complete this recovery task only after readback. A premature completion is not accepted as source progress. Preserve failed attempts as history. Remove task-owned regenerable staging after its final consumer through the ordinary scratch lifecycle; do not allocate feature worktrees for this runtime diagnosis.
