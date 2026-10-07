# Hermes orchestrator card body template

**Board/repository:** `<board-slug>` / `<repo URL and local checkout>`
**Assignee:** `orchestrator`
**Delivery-group base:** verified `origin/dev` SHA `<sha>`
**Dependent slice inputs:** `<approved clean committed predecessor SHA and verified handoff receipt, where applicable>`
**Approved planning card:** `<id>`, status `done`, reviewer PASS
**Approved artifact:** `<stored attachment path on THIS card, byte size, revision chain, SHA-256 for each effective file>`
**Approval provenance:** `<verified owner identity, source chat/message ID, date, venue rule, settled decisions>`
**Scope/exclusions:** `<exact approved work; no invented endpoints>`
**Forced skills:** `approval-gated-kanban-development`, `<project-invariant-skill>`
**Model/context/fallbacks:** inherit the `orchestrator` profile's primary route, explicit context/compression settings, and ordered `fallback_providers` from [`../profiles.json`](../profiles.json). For child cards, use **each child's own assignee profile**; never copy the orchestrator's context window or fallback route into all children.

This card may be created **only after** the exact currently reviewed plan has separate owner approval and no matching graph already exists. Verify the board/repo/profile roster and fresh `origin/dev` before creating dependent cards. Orchestrate; do not implement or reinterpret product behavior.

**Governing inputs by reference:** keep the approved plan, approval note and other governing inputs attached to **this** orchestrator card only. Each child body lists their exact paths on this card, byte sizes and SHA-256 values and instructs the child to read and verify them before any effect, blocking its own card on mismatch or absence. Do not require per-child attachment copies—workers cannot attach to other cards. Complete after the graph, edges, input references and subscriptions read back.

Preserve the reviewed, approved cohesive slice boundaries. One complete delivery is not a reason for a catch-all implementor assignment; internal phases do not fix an impractical workload. Check each slice's scope/exclusions, actual prerequisite inputs, shared-change ownership, focused verification and completed-result handoff before dispatch. Follow the [current orchestrator instructions](../role-instructions.md#orchestrator).

Use the approved delivery-group graph below. Multiple reviewed slices may share setup, integration and exact-candidate acceptance gates where the approved plan/project requirements permit; do not duplicate the complete pipeline around every small slice or omit required independent reviews:

1. Implementor cards at the reviewed slice boundaries, each in its isolated worktree/branch, parented to this orchestrator and actual approved prerequisites. Verify the slice's specified base: the delivery-group `dev` base or clean committed predecessor SHA with its receipt, never stale/uncommitted work. Implementors commit only their assigned branches and request native same-card `spec-reviewer` review.
2. Required quality/security review cards parented to their exact spec-reviewed inputs, preserving the approved ownership and independent-review contracts.
3. Prospective integration candidate card parented to **all required quality/review PASS gates**; verify every planned slice input before integration. **Actual `dev` stays unchanged**.
4. E2E card parented to candidate; test the exact candidate SHA, record real evidence and cleanup.
5. `dev` promotion card parented to E2E PASS; verify and read back the remote SHA and exact merged remote ref cleanup.
6. Separate final cleanup card parented to verified promotion, running in scratch or outside the disposable target set, with exact owned worktree/staging paths, Git registrations and local/remote refs. Intermediate retention for an actual consumer is allowed; final retention is not cleanup completion. Independently read back exact targets absent before completing this gate. Preserve only unique missing evidence in compact verified durable attachments, discard task-owned regenerable staging, and protect active/unrelated/user work and unmerged/unpublished changes. A genuine ownership/ancestry/lock obstacle leaves that SAME cleanup card unfinished/blocked, never DONE-with-retention; no force, broad purge or guessed process kills. Use the [terminal cleanup safety contract](../skills/approval-gated-kanban-development/SKILL.md#mandatory-terminal-cleanup-across-all-roles), including separate eligibility for retired failed integration attempts.

Create parent links and idempotency keys with the cards. Actively prefer concurrent ready independent slices in separate runs using the existing profiles; verify stable inputs and nonconflicting code/schema/fixture/output/shared-state ownership. Sequence actual prerequisites and conflicts, not blanket assumptions about one delivery or a shared final gate. Dependent workers receive completed immutable inputs, not stale/uncommitted work. Modest approved-scope coordination can save time; do not redesign architecture/testing for concurrency. Flag impractical approved packaging with its smallest amendment before releasing affected work, never silently collapse slices or rewrite running assignments. Populate each card's acceptance, repo/base/branch, relevant forced skills, evidence, failure route, and forbidden actions. Verify IDs, parents, roles, skill loading, workspaces, and channel subscriptions before completing orchestration. A reviewer or E2E failure must block the downstream gate and route remediation to the original implementor; a new SHA invalidates affected reviews/E2E.

**Absolute exclusions:** no implicit `main` release, no deployment, no production changes, no duplicate graph, no credentials in task bodies. A fresh authenticated owner instruction is required later for any `main` write.
