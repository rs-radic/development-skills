---
name: approval-gated-kanban-development
description: Use when running approval-gated Hermes Kanban work.
version: 1.1.0
author: rs-radic
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [hermes, kanban, approval, development]
    related_skills: [plan-review-gate, sdlc-review]
---

# Approval-gated Hermes Kanban development

Hermes injects `KANBAN_GUIDANCE` into dispatcher-owned workers when `kanban_show` is available; the former bundled `kanban-worker` and `kanban-orchestrator` skills are retired and must not be installed or forced. This skill defines **reusable governance**, not a project's product specification. Force-load a separate project-invariant skill on cards when necessary. Review the installed Hermes commands after upgrades; do not replace a richer, established local copy without comparing it.

## Plan before execution

1. Main chat targets the explicit project board and checks for an existing planner card; it does not select the substantive scope if a planner owns that decision. Create a neutral `planner` card in scratch with source pointers, exclusions, desired deliverable, and `plan-review-gate` forced. Subscribe the originating chat. Planning alone never creates execution cards.
2. The planner attaches an immutable plan (or an evidenced no-work inventory), reads the attachment back, records its SHA-256, and calls `kanban_request_review(reviewer="plan-reviewer")` **on the same card**. The reviewer checks independent sources and either calls `kanban_request_changes` for a new linked revision or completes with PASS. PASS only means the artifact is ready for human decision.
3. Before execution, verify the current reviewed attachment and every active predecessor/delta, their hashes, board status, approver identity/message, any decisions and venue rule, and absence of an existing execution graph. A changed plan needs new review and approval. If authority is unclear, ask a specific question; no implied approval from a status message.

## Orchestrate after human approval

- The orchestrator creates cards **with parents at creation** and idempotency keys, verifies actual card IDs, assignees, skills, workspaces, dependencies, subscriptions, and no automatic `main` path. Generic roles serve multiple boards; board-specific behavior belongs in project cards/skills.
- Each independent feature/fix uses its own branch and worktree against a verified current `origin/dev` base. Native `wt/<task-id>` naming is fine; do not rename worktrees behind live task metadata. The implementor pushes only that lane branch and requests same-card `spec-reviewer` review. The reviewer does not edit code: approve or return changes on the same card.
- Downstream graph: independent quality/security review → separate prospective integration candidate (actual `dev` untouched) → E2E against the exact candidate SHA → promotion of only the passing candidate to `dev` → separate guarded worktree cleanup. Independent implementation lanes may run in parallel; serialize shared integration/promotion dependencies.
- A downstream review failure cannot be marked `done` merely to unblock integration. Add a remediation dependency for the original implementor, retain the feature branch, rerun native spec review and affected downstream checks against the corrected SHA. E2E failure leaves `dev` unchanged and requires new candidate and full E2E. Re-enter orchestration only for changed requirements, dependencies, or architecture.
- Promotion verifies the tested SHA, confirms base has not drifted, pushes `dev`, reads remote SHA, and handles only exact proved-merged remote refs. Guarded local cleanup happens separately after workers release their paths; no force removal, broad pruning, or deletion of ambiguous branches.

## Release boundary and validation

No standing release card or worker writes `main`. Require a fresh, authenticated owner instruction naming the intended release state; recheck remote refs and evidence at release time. Project-specific venue and approval scope may be narrower than this generic rule.

Before real code: verify each profile's canonical primary and ordered fallback provider/model routes, effective reasoning, explicit primary `model.context_length`, compression ratio/cap (including omitted keys), effective context and compaction after a fallback, force-loaded skill versions, planning/reviewer no-op handoff, complete no-op DAG/notifications/gates, safe worktree base, no implicit fallback to the main-chat profile, and no path to `main`. Preserve these non-secret settings in a reusable profile snapshot; omit credentials and project-specific skill contents, not context defaults or fallback models. Never describe a dry-run as real application E2E.

## Shared-profile maintenance and helper budgets

Before dispatch, compare canonical workflow skill bytes and required references across affected profiles, including native reviewers. Follow the bounded [shared-skill repair procedure](../../skill-routing.md#maintain-shared-workflow-skills): use separate standing maintenance authority, explicit file/profile allowlists, private backups, and installed-loader verification; preserve unknown local edits and running workers. Repair known drift on encounter without creating scheduler infrastructure or an all-project-idle gate. Stored-file propagation does not rewrite an already-loaded conversation or grant new product/release authority. Continue only the existing graph and independently verify any resumed claim.

Read the dated `delegation_snapshot` in `profiles.json` independently of model/context settings. `oneshot_max_children` is each parent agent's cumulative direct-child budget in a one-shot run; completed children do not replenish it. In the observed build, `max_concurrent_children` limits tasks and parallelism per delegation call and is also reused for background call/batch-slot admission across the process, not a hard aggregate per-parent or whole-tree child ceiling. Multiple admitted background batches can collectively exceed that child count, although a worker's separate finite one-shot budget still applies. `max_spawn_depth` controls permitted nesting; nested parents have separate cumulative counters and may increase aggregate descendant counts. These profile-scoped helper limits do not replace Kanban card-concurrency caps, dependencies, independent reviews, or approval gates. A shared-profile change affects other boards; fresh resolver evidence does not prove adoption by an existing worker.

## Do not

- Treat `kanban.auto_decompose` as a board-local setting; explicitly assign cards rather than changing global behavior for one board.
- Copy credentials, a live board database, logs, sessions, or profile `.env` files into a template.
- Equate a review PASS, approved plan, passing unit tests, or a worker's self-report with exact-candidate E2E or release authority.
- Assume a corrected SHA inherits review evidence from the prior candidate.
