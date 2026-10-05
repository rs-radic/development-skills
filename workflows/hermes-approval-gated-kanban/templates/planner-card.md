# Hermes planner card body template

**Board:** `<board-slug>` (explicit; do not switch the installation's default board)
**Assignee:** `planner`
**Workspace:** `scratch`
**Forced skills:** `plan-review-gate`, `<project-invariant-skill>` if applicable
**Model/context/fallbacks:** inherit the `planner` profile's primary route, explicit context/compression settings, and ordered `fallback_providers` from [`../profiles.json`](../profiles.json); a card-level model/provider override requires fresh effective-context verification.
**Source/authority:** `<exact source document paths or attached IDs, versions and hashes>`
**Current board exclusion set:** `<completed, approved-but-queued, in-flight, and unapproved-proposed scopes>`
**Request:** `<neutral scope question; do not preselect substantive answer>`

Plan **only**. Inspect source material and current board; select a bounded eligible slice independently, or attach an evidenced no-work inventory. Do not edit product code, create an execution graph, push refs, run live integrations, deploy, or decide unresolved product questions for the owner.

Apply the [planning and review policy](../skills/plan-review-gate/SKILL.md#simplicity-and-sufficient-testing): choose the simplest correct implementation using existing project patterns, proportionate steps, and minimum sufficient coverage of required features, relevant failures, and required real E2E workflows. Reuse tests and infrastructure; duplicate layers only when they prove distinct risks. Generic template granularity is not permission to add speculative scope or waive owner decisions, project requirements, existing protections, independent review, or E2E gates. The plan reviewer must identify a concrete material defect, missing required coverage, or clearly avoidable complexity to request changes; an already correct and proportionate plan needs no extra recommendations.

Required artifact: an immutable plan with scope/exclusions, provenance, unresolved decisions, dependency-aware implementation cards, acceptance criteria, test/E2E and cleanup strategy, risk and rollback. Attach the exact file to **this card**, read back stored bytes, record its SHA-256 and revision chain, then call `kanban_request_review(reviewer="plan-reviewer")` on this **same card**. Do not complete the card yourself. If the reviewer requests changes, retain the earlier artifact and attach a linked revision for fresh review. An independent PASS is not owner approval.

**Slice and scheduling contract:** follow the [current planner/reviewer instructions](../role-instructions.md). Plan the complete requested scope as cohesive executable slices, each with exclusions, prerequisite inputs, shared-change ownership, focused verification and completed-result handoff. Avoid catch-all assignments, substantial work hidden inside internal phases, and per-file/method fragmentation. Account for investigation/coding/testing effort; actively design stable-input, nonconflicting slices for concurrent completion. Establish shared prerequisites once and give concrete reasons for material serial bottlenecks. Weigh coordination/integration/rework costs, allowing modest scope-preserving coordination without adding major architecture merely for parallelism. Reuse approved shared setup/acceptance gates without weakening verification or copying the entire pipeline around each small slice.

**Mandatory terminal cleanup:** name the separate final cleanup card, exact owned worktree/staging paths, Git registrations and local/remote temporary refs, evidence placement outside disposable worktrees, and cleanup ownership. Intermediate retention may protect an active reviewer/tester; the final cleanup gate must independently verify removal, not complete with retained assets. Reuse durable attachments and preserve only missing unique required evidence compactly; do not archive whole SDK/cache/build trees. Genuine ownership/ancestry/lock obstacles leave that same final gate unfinished, with active/unrelated/user work and unmerged/unpublished changes protected. Planning/review checks this contract; it does not perform deletion or rewrite a live graph.

**Notification:** subscribe the source chat to terminal and decision-required events on this card; verify the subscription.

Example CLI shape (fill the body file and board slug yourself):

```text
hermes kanban --board <board-slug> create "<neutral title>" --assignee planner --workspace scratch --skill plan-review-gate --body-file <absolute-template-filled-file> --idempotency-key <stable-request-id>
```
