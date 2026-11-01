# Structural progress and delivery recovery

This reference describes source `4e598dd4a9ca2875b004d30740a6a33cbf15a0af`. It extends the existing [recovery contract](recovery.md), not operator authority. The same `recovery enable`/`disable` gate and read-only `recovery-check` command apply. There is no `progress enable` command, new configuration flag, automatic activation or universal recovery guarantee. The operator must first supply the approved project workflow/recovery contract, compatible Windows Hermes installation and capable non-worker coordinator.

## Structural and outer diagnostics

`progress.py` first projects the current planning/publication handoff: request identity, actual runs/parents/children, relevant reviewed plan name/hash, local transitions, input generations and approval. A finished producer is not proof that its required output was published or its continuation acquired an owner. Invalid handoff metadata remains invalid; a diagnostic never fabricates PASS, substitutes a hash or grants development approval.

The structural path preserves ordinary queues, active successor/reviewer ownership, current human waits and recursive question/fence/prerequisite obligations. Exact integration-owned definite-failure transitions can be diagnosed without clearing their holds or retrying the refused operation. A never-sent initial request has a logical `progress-<task>-request` anchor with run 0; returned, attempted or ambiguous creates do not prove native absence.

When a specialist cannot establish continuation, the outer catch-all observes eligible nonterminal work through the existing **ten-minute unchanged-observation grace**. It uses `progress-<task>-workflow`, run 0. It is diagnostic liveness, not a business-readiness or execution predicate: lack of product readiness need not hide an automation stall. A verified current human wait normally suppresses this diagnosis, but an independent delivery failure can still require ownership. Operator holds and deliberately disabled staged handoff remain respected. Retry counters, leases and polling timestamps do not by themselves count as meaningful progress.

Missing native evidence remains **unknown**, never an empty or healthy graph. The outer path may diagnose an observation failure while preserving its inactivity clock when local context has not changed. That admits observation-failure diagnosis only, not a native source operation. Its context is rechecked at wake completion and through public `recovery-check`; logical anchors never fall through to native card lookup. Null approval/revision/hash are rendered as JSON `null` and convey no product execution authority.

All progress ACKs go on the actual recovery carrier, not on a logical anchor. Only `CURRENT` admits diagnosis; `STALE` requires stopping and `UNAVAILABLE` requires stopping/re-observing. The coordinator still needs the existing operation-specific authority and fresh closure checks. Never clear an operator/security hold, bypass a dependency/ticket, complete a question, replace a graph, reset history, edit lifecycle SQL or repair integration code merely because a wake arrived.

For a completed planner with an invalid handoff, the carrier describes a narrow supported same-request requeue: the authorized operator must freshly verify the request is still terminal, with no descendants or active owner. Preserve original plan bytes and malformed metadata in history. A fresh review request must bind the correct hash of those unchanged bytes, and a real independent reviewer must review again. The wake is not permission to rewrite history, invent review evidence or broaden the project's recovery scope.

## Completed-workflow retirement

A workflow is retired only when current approval and reviewed plan evidence, the declared promotion/cleanup graph, all observed native completion states, and the delivered final status agree. Open questions, unfinished effects, live runs, unknown descendants or missing proof prevent retirement. A later independent obligation gets a new logical anchor after the completed incident; fingerprint changes within unfinished work do not reset budgets. Nested collection reuses discovered IDs only, reacquires current state, and reserves reads for fresh admission.

## Current dependency observation is not new authority

`engine.dependency_scope` starts from current approval-bound declared/recovery roots. `dependency_members` follows required native ancestors, so a later-added repair dependency and its own question can be observed even when absent from the original declared lineage. The mapping is evidence-only: it does not rewrite approved lineage, graph edges, card bodies or stored development approval.

The native closure requires complete reciprocal edges, unique approval ownership and a bounded acyclic graph. Foreign/shared/ambiguous ownership, approval gates and question/fence/park protocol cards cannot become new worker targets. Unknown required reads defer; they are not affirmative detachment. Closed-card caches are hints, because dependencies can be added later. Current collection, question detection/delivery, recovery effects and public admission use this validated mapping; unavailable native proof does not silently fall back to a weaker legacy observation path.

The native snapshot bounds are 512 dependency nodes and 4,096 edges, sharing the existing 64-read allowance and proof freshness checks. These are diagnostic/collection limits, not a claim that every valid graph can finish within them. Required ancestry stays authoritative even behind a completed intermediate parent: an active or unreadable ancestor still defers source operations. A narrow undelivered-question exception covers that question's stopped internal delivery only, not its ancestry, live runs or another dependency. Verified unanswered human questions remain human waits.

Old `superseded` blocker rows remain audit history rather than a veto on current pre-worker staging. This does not ignore a still-current native dependency, a current question or a fence. Current approval/generation and actual obligations must still be checked before any effect.

## Status-reply delivery and explicit repair

Normal answer-status replies use the closure comment's thread root. An answer incorporated by a newer reviewed and explicitly approved plan can have no separate closure item. Only in that case, `_closure_root` uses the exact delivered original question: a completed same-task `cu_comment` outbox row whose provider root is observed as an undeleted top-level comment on the originating task. No guessed root, arbitrary replacement comment or top-level-post fallback is allowed. Destination refusal stays a refusal.

`outbox resend <exact-effect-id>` has one additional, explicit repair for the historical never-sent NULL-root `Answer recorded` or resumption status. It is not an automatic replay or a repair of the answer itself. The command requires all of the following:

- The exact base status key, `cu_reply` kind, original canonical body/hash and dependency/precondition shape, with `cancelled_stale/FOREIGN_ROOT`.
- Zero attempts; no first-send, child, next-attempt or result state; no transport mirror. A possibly sent, ambiguous or modified effect cannot use this path.
- An eligible, unheld, nonterminal task; current fully classified input/scope; the exact applied incorporation, governing approval, delivered generation and published revision/hash; no newer active approval.
- The unchanged exact question/work binding and a verified usable original question root.

The repair saves the prior row in local delivery-repair history and rearms the same status row with fresh authority preconditions. Its text explicitly labels a **historical update for the earlier question**, not a claim about current work status. Subsequent live polling still validates and sends/reconciles it. Rearming does not release a worker, redeliver/consume an answer, clear a hold or reset any recovery deadline. The generic outbox retry command is not permission to loosen these checks or redirect a rejected reply.

## One diagnostic owner, including temporary delivery ownership

A failed ClickUp notification is independent of native worker health. `failed` originating-task effects and exact `FOREIGN_ROOT` cancellations can become delivery-only diagnostics even while a worker progresses or a verified human question is waiting. Diagnostic notices themselves are excluded from recursively creating another delivery-failure incident. A queued notice or local wait flag does not prove provider delivery.

If an existing specialist/source incident already owns the task, the implementation can temporarily reuse it for the independent delivery obligation only after verifying the unchanged question root, done base/mirror readbacks, observed provider comment, wait digest and progress binding. It preserves the incident identity, original class/history, question, approval, spent attempts and absolute deadline; it does not create a competing owner or refund a budget. Applicable exact failed-effect/mirror holds admit delivery-only diagnosis, never source execution. Operator/unrelated holds remain binding. Public admission explicitly reports `scope=delivery-only` for that role.

Temporary ownership also has an exit lifecycle. Once its delivery obligation resolves, the original answer enters classification/answer handling, or positively verified newer approval supersedes the old owner, the role is retired to history and the original class/guarded lifecycle is restored. A superseded source owner can then be marked superseded; staging continues only through its normal approved path. Missing observations, stale admission alone or a merely claimed worker are not cleanup evidence. A previously diagnosed delivery fingerprint cannot repeatedly reacquire the role to evade the original lifecycle or budget.

The existing three-wake and absolute 24-hour bounds remain. A supported handoff, changed fingerprint, later claim or replacement notice is not a new allowance. `cannot_act`, carrier completion, warnings and queued explanations are intermediate, not recovered work. Actual authorized capable-owner progress or a verified genuinely human-required question with a usable bound reply path must be distinguished from unresolved automation/delivery. A technical explanation must not ask the user to repeat an already supplied product decision.

## Source and evidence map

- `progress.projection`, `outer`, `detect`, `step`, `verdict`: selection, grace, bounded continuation and fresh diagnostic admission.
- `progress.continue_incident`, `delivery_current`, `retire_delivery`: one-owner continuation and temporary delivery-role lifecycle.
- `engine.dependency_scope`, `dependency_members`; `main._observe_dependencies`, `_recovery_dependencies`: current required dependency observation and effect-time/public checks.
- `engine._approved_plan_resolution`, `_closure_root`; `main._resume_blocker_notification`: narrow approved-plan incorporation and status-delivery repair.
- `templates/progress_card.md`: rendered coordinator procedure, null authority and carrier ACKs.
- New focused tests cover these contracts; their presence is not execution evidence. Current results, historical failures and NOT_RUN qualifications belong in [verification notes](verification.md) and [known test qualifications](known-test-failures.md).
