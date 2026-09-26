# Hermes orchestrator card body template

**Board/repository:** `<board-slug>` / `<repo URL and local checkout>`
**Assignee:** `orchestrator`
**Target base:** verified `origin/dev` SHA `<sha>`
**Approved planning card:** `<id>`, status `done`, reviewer PASS
**Approved artifact:** `<stored attachment path, revision chain, SHA-256 for each effective file>`
**Approval provenance:** `<verified owner identity, source chat/message ID, date, venue rule, settled decisions>`
**Scope/exclusions:** `<exact approved work; no invented endpoints>`
**Forced skills:** `approval-gated-kanban-development`, `<project-invariant-skill>`

This card may be created **only after** the exact currently reviewed plan has separate owner approval and no matching graph already exists. Verify the board/repo/profile roster and fresh `origin/dev` before creating dependent cards. Orchestrate; do not implement or reinterpret product behavior.

For each independent lane, create at minimum:

1. Implementor feature/fix card in isolated worktree/branch, parented to this orchestrator; implementor commits only its branch and requests native same-card `spec-reviewer` review.
2. Quality/security review card parented to the spec-reviewed implementation card.
3. Prospective integration candidate card parented to quality PASS; **actual `dev` stays unchanged**.
4. E2E card parented to candidate; test the exact candidate SHA, record real evidence and cleanup.
5. `dev` promotion card parented to E2E PASS; verify and read back the remote SHA and exact merged remote ref cleanup.
6. Guarded local-worktree cleanup card parented to verified promotion, without force or blanket cleanup.

Create parent links and idempotency keys with the cards. Parallelize only independent lanes; express real cross-lane dependencies explicitly. Populate each card's acceptance, repo/base/branch, relevant forced skills, evidence, failure route, and forbidden actions. Verify IDs, parents, roles, skill loading, workspaces, and channel subscriptions before completing orchestration. A reviewer or E2E failure must block the downstream gate and route remediation to the original implementor; a new SHA invalidates affected reviews/E2E.

**Absolute exclusions:** no implicit `main` release, no deployment, no production changes, no duplicate graph, no credentials in task bodies. A fresh authenticated owner instruction is required later for any `main` write.
