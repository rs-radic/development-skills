---
name: approval-gated-kanban-development
description: Use when running approval-gated Hermes Kanban work.
version: 1.2.0
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

- Preserve the reviewed, owner-approved **cohesive slice boundaries**; a single whole-project delivery is not a reason to collapse them into one implementation card. Each slice names its scope/exclusions, immutable prerequisite inputs, shared-change ownership, focused verification, and completed-result handoff. Size around investigation, coding, and test effort—not arbitrary file/line/time quotas or maximum worker count. Actively prefer the shortest practical path to tested delivery, including coordination, reviews, integration, and likely rework. Establish common prerequisites once and require concrete reasons for material serial bottlenecks.
- Run ready independent slices in separate runs/worktrees, reusing existing profiles. Check stable inputs and nonconflicting code, schema, fixture, output, and shared-state ownership. A common integration, E2E, promotion, or cleanup gate does not require serial coding. Modest scope-preserving coordination can be worthwhile; major architecture, interface, behavior, or acceptance changes still require approval. When approved packaging is impractical, request the smallest packaging amendment before releasing affected work; do not rewrite running assignments or duplicate their graph.
- Check actual dispatcher/profile capacity and live ownership before promising parallel execution; separate worktrees do not isolate shared services or fixtures. Eligible or queued work is not verified running work. Preserve active workers and do not change limits or scheduling as a documentation side effect.
- The orchestrator creates cards **with parents at creation** and idempotency keys, verifies actual card IDs, assignees, skills, workspaces, dependencies, subscriptions, and no automatic `main` path. Generic roles serve multiple boards; board-specific behavior belongs in project cards/skills.
- Each independent feature/fix uses its own branch and worktree with an explicit verified base. `origin/dev` is the delivery-group base; an approved dependent internal slice may instead start from its clean committed predecessor SHA and verified handoff receipt. Do not substitute stale or uncommitted inputs, or reset an existing retry's work to force a base. Native `wt/<task-id>` naming is fine; do not rename worktrees behind live task metadata. The implementor pushes only that slice/lane branch and requests same-card `spec-reviewer` review. The reviewer does not edit code: approve or return changes on the same card.
- Downstream graph: independent quality/security review → separate prospective integration candidate (actual `dev` untouched) → E2E against the exact candidate SHA → promotion of only the passing candidate to `dev` → separate mandatory final cleanup. Preserve all required gates, while reusing shared setup and delivery-group acceptance gates where the reviewed plan and project requirements permit; do not duplicate the entire acceptance pipeline around every small slice. Independent implementation lanes may run in parallel; serialize genuine shared integration/promotion dependencies.
- A downstream review failure cannot be marked `done` merely to unblock integration. Add a remediation dependency for the original implementor, retain the feature branch, rerun native spec review and affected downstream checks against the corrected SHA. E2E failure leaves `dev` unchanged and requires new candidate and full E2E. Re-enter orchestration only for changed requirements, dependencies, or architecture.
- Promotion verifies the tested SHA, confirms base has not drifted, pushes `dev`, reads remote SHA, and handles only exact proved-merged remote refs. Local cleanup remains a separate gate after actual consumers release their paths; its completion requires independent absence readback, not safe retention. Follow the mandatory cleanup policy below.

## Implement the smallest correct solution

Inspect and reuse existing code, architecture, dependencies, and patterns. Implement the full approved card behavior without speculative features, unrelated refactors, single-use abstractions, or future-proofing. Use minimum sufficient tests for required behavior and relevant failures; reuse existing infrastructure and add a layer only for a distinct risk or explicit requirement. Do not add production abstractions, endpoints, or elaborate scaffolding merely to suit a preferred test technique.

Preserve existing tests, security, data integrity, source-faithful contracts, and mandatory real application/E2E checks. Report exactly what ran and what remains downstream. Correct introduced in-scope defects; report pre-existing or adjacent defects separately without self-authorizing their repair. Apply the smallest adequate review correction rather than redesigning the feature. Submit the verified candidate for independent review once requirements and required checks are complete; do not keep adding unsupported polish. A simpler implementation detail must retain approved behavior/contracts; changing approved architecture, interfaces, behavior, or acceptance needs approval.

## Mandatory terminal cleanup across all roles

Plan the separate final cleanup card and exact path/ref absence criteria from the outset. Planning/review roles define or check this criterion; only the authorized execution/cleanup roles perform disposal. Intermediate cards may retain a worktree while an actual reviewer or tester needs it, but **retention is not final cleanup completion**. This policy supersedes older wording that allowed a final cleanup gate to complete with safely retained lane assets.

Before removal, reuse existing durable attachments and move only missing unique required evidence into compact, byte-verified durable artifacts outside disposable worktrees. Do not archive whole SDK/cache/build trees instead of reclaiming space; discard only task-owned regenerable runtime/build/cache staging. Evidence preservation is not authority to discard unmerged or unpublished code.

For each exact owned target, verify ownership, completed consumer handoffs, current remote/ref SHAs and ancestry, **both worktree HEAD and local branch tip**, tracked/untracked content, unique unpushed changes, locks, and actual process holders. Protect active/unrelated work, primary/user files, credentials, and unmerged/unpublished changes. Resolve only verified owned holders through supported scoped operations; no guessed kills, broad purge/prune, forced removal, or ambiguous branch deletion. Recheck immediately before each supported nonforced Git removal. Run final cleanup outside the disposable target set, rather than trying to delete the cleanup worker's active workspace.

A retired failed integration attempt is not automatically merged by a different candidate's promotion. Require its separate recorded eligibility: exact failed SHA/ref/path and failure evidence, a superseding proven candidate on remote `dev`, source/parent commits reachable there, no unique unreconciled product work or missing evidence, and no active consumer/lock. An ambiguous unmerged attempt stays protected and the same final cleanup gate stays unfinished; do not force it through ordinary merged-ancestry cleanup.

Independently read back every retired lane worktree path, Git registration, owned local feature/candidate/remediation ref, owned remote temporary ref, and task-owned disposable staging target as absent; verify promoted `dev` and protected release refs remain correct. A genuine ownership, ancestry, provenance, or lock obstacle preserves that target and leaves the **same final cleanup card unfinished/blocked** until resolved, never DONE-with-retention. Promotion PASS is distinct from final cleanup PASS. Do not restart the gateway to apply these file instructions or retrofit a live graph as a publication side effect. The [all-role instruction snapshot](../../role-instructions.md) records each role's adopted policy and boundaries.

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
