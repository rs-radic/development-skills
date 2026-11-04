---
name: plan-review-gate
description: Use when a Hermes Kanban plan needs independent review.
version: 1.3.0
author: rs-radic
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [hermes, kanban, planning, review]
    related_skills: [approval-gated-kanban-development, sdlc-review]
---

# Independent Hermes Kanban plan review

Apply only to **new, explicitly opted-in planning cards**; do not retrofit a completed or running card. Hermes dispatches the independent reviewer on the same task's native `review` lane. This procedure reviews a proposal, **not** implementation code; product approval remains with the owner.

## Simplicity and sufficient testing

The following policy records the planner and plan-reviewer profile instruction additions observed on October 4, 2026. The shared template-precedence rule applies to both roles.

### Planner policy

Plan the simplest correct implementation that fully satisfies the requested requirements. Prefer existing code, architecture, dependencies, and project patterns. Do not introduce abstractions, configuration, infrastructure, or future-proofing unless a current requirement makes them necessary.

Use the minimum sufficient test coverage: verify the required features, relevant failure cases, and required real E2E workflows. Reuse existing tests and test infrastructure. Avoid duplicating coverage across test layers unless each layer proves a distinct risk. Preserve project-specific verification requirements and existing protections.

Keep the plan proportionate to the change, with meaningful implementation steps rather than excessive decomposition.

### Plan-reviewer policy

Review the plan for correctness, completeness, and simplicity. Ask: **Can this be implemented or tested more simply without losing required behavior, coverage, security, or E2E verification?**

If so, identify the specific unnecessary work and propose a simpler alternative. Do not add speculative requirements or extra test machinery merely for completeness. Request changes for material defects, missing required coverage, or clearly avoidable complexity—not personal architectural preferences.

If the plan is already correct, sufficiently tested, and proportionate, approve it without adding recommendations.

Here, approving the plan means an independent **PASS suitable for owner review**, not product approval, permission to implement, or release authority. Keep the required artifact hash, source trace, checks and limitations in the verdict; the absence of extra recommendations does not remove that evidence.

### Template precedence for both roles

Apply these rules when interpreting generic planning and test templates: template granularity or duplicate-coverage checklists alone do not justify expanding the plan. Explicit user decisions, project-specific requirements, existing tests, security controls, and mandatory independent-review and E2E gates remain binding.

## Practical slices and independent scheduling review

The [current role instructions](../../role-instructions.md) extend the October 4 simplicity policy with practical workloads, faster completion, and mandatory final cleanup. Plan the complete requested scope as cohesive code-plus-focused-verification slices, not one catch-all assignment or fragments per file/method. Each slice identifies exclusions, prerequisite inputs, shared-change ownership, verification, and completed-result handoff; account for investigation and test effort rather than arbitrary quotas. Establish shared prerequisites once, actively identify safe concurrent work, and justify material serial dependencies with concrete ownership or input constraints.

Optimize end-to-end tested delivery—including setup, coordination, reviews, integration and likely rework—not the fewest cards or most workers. Stable code/schema/fixture/output/shared-state ownership is necessary for concurrency. Modest scope-preserving coordination may save time; avoid major architecture or test machinery just to enable parallelism. A common final integration/E2E/promotion/cleanup gate is not a reason to serialize independent coding. Reuse shared setup and acceptance gates where approved, without dropping mandatory verification or creating a complete duplicate pipeline per small slice.

The independent plan reviewer checks workload and scheduling as well as correctness and simplicity. Challenge oversized cards, substantial work hidden in internal phases, excessive fragments, missing ownership, unassigned behavior/verification, unsafe concurrency, and unjustified serial bottlenecks. Propose specific minimal packaging corrections, not scope reduction, weaker tests, invented architecture, or another acceptance pipeline. PASS requires a complete, proportionate, executable plan and remains suitable for owner review only.

Every proposed delivery group names its separate final cleanup gate, exact retired paths/refs, evidence placement outside disposable worktrees, and cleanup ownership. Check that active consumers and unique/unpublished work stay protected. Intermediate retention may be necessary; **final retention is not cleanup completion**. Genuine obstacles must keep the same cleanup gate unfinished until verified removal. The planning reviewer checks this contract without deleting assets or rewriting an existing graph.

Reserve cleanup ownership before resource creation, including report staging in a read-only phase. If a later approved phase reuses a completed gate, specify its idle check, permitted reopen, unfinished parent, updated ownership and readbacks; preserve prior receipts and avoid a cycle with a pending cleanup-produced input. Before early retirement, queued/review consumers must be non-dispatchable, not merely absent from the running list. See the [cleanup contract](../approval-gated-kanban-development/SKILL.md#mandatory-terminal-cleanup-across-all-roles).

## Main chat

- Check the exact board for a matching planner card; avoid duplicate planning or execution graphs. Create a neutral, planning-only scratch card with original source documents, scope, exclusions, and `plan-review-gate` force-loaded, assigned to `planner`. Verify the skill resolves in the actual planner and reviewer profiles before pinning it; a default-profile copy proves neither. Subscribe the originating chat. For an independently requested plan from requirements, omit an earlier assistant solution and its derived decision bundle rather than anchoring the planner to it.
- Require the planner to attach an immutable plan or evidenced no-work inventory, read back stored bytes and hash, then call `kanban_request_review(reviewer="plan-reviewer")` on the **same card**, not `kanban_complete`.
- When the reviewer completes with PASS, read the complete exact reviewed attachment and active predecessors, verify hashes and status, deliver those bytes, summarize remaining owner decisions, and obtain separate owner approval before execution. Recover whole-file or per-line truncation before claiming a complete read. Do not edit a reviewed header merely to announce PASS. Verify actual destination delivery rather than equating a board attachment with a chat attachment. An open `review` card or planner handoff is not a PASS.

## Approval and immutable handoff

- Bind each answer to its actual pending question, authenticated source, venue and presented artifact hash. Preserve selected options, exclusions and phase limits in a compact cumulative receipt. Resolve only a genuine ambiguity; do not reopen settled choices or require individual votes on compatible technical defaults. Planning, exact-plan execution, test exceptions, external capability use and deployment remain distinct authorities. An unambiguous execution approval may proceed while a separately excluded finding remains discussion-only.
- Approval belongs to the revision presented, not automatically to a newer attachment. Check intervening owner amendments and conflicting conditions before releasing work. A substantive revision needs independent review and exact approval without re-asking unchanged decisions. For an active planning/review card, relay changes at a safe checkpoint; do not overwrite a running author's brief or restart a healthy worker. Preserve frozen drafts as history and require the native author and reviewer to incorporate the new scope.
- Keep one operative contract. Make a narrow correction in a new immutable revision, replacing contradictory clauses rather than appending overlapping rules. Preserve passing sections where possible and retain predecessors. For an amendment, deliver the complete required reading chain and a clearly non-authoritative navigation/index; do not rewrite approved bytes into a new plan. Validate Markdown table cell counts outside code fences; inline backticks do not protect a literal pipe, so escape it or reword the cell.
- Stage the approved orchestrator behind an initial hold when input writes are separate, verify its artifacts, authority receipt, parents and subscription, then release that same card. Follow the [approved-work handoff](../approval-gated-kanban-development/SKILL.md#bind-and-stage-approved-work) for conditional milestones and read-only subsets. No-work findings need no execution graph, and a later unapproved phase must not start automatically.

## Planner

1. Independently inspect the source record and board, distinguishing approved behavior, implementation choices, in-flight work, unapproved proposals, and exclusions. Choose only eligible scope. If none exists, produce an evidenced no-work map rather than invented work.
2. Include dependencies, API/data/rollback effects where applicable, boundaries and unanswered product decisions, card DAG, branch lanes, acceptance criteria, security, unit/integration/E2E and cleanup evidence. Establish the permitted workspace setup owner and creation order: a worker cannot repair a checkout it needs before starting. Identify input/cleanup readiness holds and who may release them before first claim. Cross-project planning can proceed in parallel; implementation depends on actual delivered prerequisites, not another orchestrator's completion. Do not implement, change other cards or assume authority over another project's work.
3. Attach the proposal, read back exact bytes, record SHA-256, and request native review by `plan-reviewer` with artifact path/revision/source metadata. Where an integration consumes structured receipts, verify its exact field names, types, revision and input-generation bindings before requesting review; later PASS prose does not repair a malformed producer request. On changes requested, write a **new linked revision**, retain prior attachments and explain supersession; request review again. Treat unchanged-artifact upload or metadata repair as transport work, not permission to rewrite the plan or fabricate a verdict. Block only for genuine owner decisions.

## Conditional and asynchronous plan checks

Apply these static checks only where the proposal has the corresponding risks; they do not create a new test campaign or implementation permission.

1. **Source and evidence contracts.** Trace proposed operations through actual callers and effects, not HTTP method names or a preferred design. Distinguish immutable historical evidence from current mutable ledgers/state; preserve historical hashes and reconcile current state separately. Carry existing owner exceptions at their exact assertion scope as NOT RUN by exception, never PASS. A substitute observation does not satisfy an unperformed mandatory check without that authority.
2. **Executable alternatives.** Review each offered option's activation order, acceptance, side-effect limits, rollback and cleanup. An unworked alternative is not already approved by the review. Make compatible integration-drift handling executable where adopted, preserve explicit freezes, and do not turn routine technical waits into new product choices.
3. **Conditional consistency.** Map triggering evidence and observation time to each permitted next action, preserved state, fallback owner and stop condition. Separate success, confirmed no mutation, mutation followed by failure and unknown outcome. Count requests, sessions and mutations separately along every reachable branch, including earlier spent attempts and final readbacks; reconcile any branch exceeding its approved cap. Reference one policy from work orders and check every copied fallback, budget and cleanup clause for contradiction, including interruption of recovery itself.
4. **Asynchronous boundaries.** Trace producer, persisted intent, first consumer start, delivery, actual consumption and acknowledgement. Bind answers and resumed work to the current input, stopped run and approval; recheck at action time, not only queue time. Cover first-run, duplicate/delayed events, startup failure, newer answers and completion-before-parking. A claim, PID, heartbeat or closed question does not prove the worker consumed the right input.
5. **Holds and recovery.** Preserve non-final delivery/resumption obligations across temporary holds; keep cancellation and terminal dispositions separate. Reconcile possibly applied operations before retrying and use the supported transition for the actual returned state. If atomicity or automatic continuation cannot be established within authority, expose that precise gap; do not disguise a manual alert, compensating re-block or generic unblock as the promised mechanism.
6. **Isolation and compatibility.** Resolve fixture storage, profile/worker pins, notification destinations and shared-state ownership before the first test write. Where rollout promises compatibility, examine previous readers against candidate-written state and coherent code/template activation separately from new-state admission. Do not broaden runtime permissions to make a design pass. Label static/design probes separately from executed candidate and installed acceptance evidence.

## Independent plan reviewer

1. Verify the `review` lane and reviewer assignment. Independently inspect original sources, complete stored proposal and predecessor revisions, their hashes, current board, and applicable project rulings—not just the planner's summary. Do not edit artifacts, code, refs, or other cards.
2. Trace material assertions to source: scope and omissions, ownership boundaries, choices vs requirements, existing or queued work, true dependencies and promotion order. Apply the conditional/asynchronous checks to both narrative and executable work orders. Check risk-bearing capacity, retry/expiry, authorization, data minimization, concurrency, rollback with retained data and exact-candidate E2E only where applicable. Say when a check is not applicable.
3. On a correctable issue record exact evidence and `kanban_request_changes` **on that same task**. A needed business decision is blocked for owner input; do not guess. On PASS call `kanban_complete` with artifact SHA-256, sources, evidence and limits. PASS never authorizes implementation.

## Completion check

The task shows planner `review_requested` and an independent reviewer PASS at the same attachment hash, or a recorded changes-requested/revision/re-review trail ending in PASS. The project repo, live execution graph, `dev`, `main`, and deployment were untouched by the planning gate.
