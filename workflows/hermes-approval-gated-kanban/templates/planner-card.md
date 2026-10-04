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

**Notification:** subscribe the source chat to terminal and decision-required events on this card; verify the subscription.

Example CLI shape (fill the body file and board slug yourself):

```text
hermes kanban --board <board-slug> create "<neutral title>" --assignee planner --workspace scratch --skill plan-review-gate --body-file <absolute-template-filled-file> --idempotency-key <stable-request-id>
```
