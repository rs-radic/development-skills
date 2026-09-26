# Sanitized example: Partner Network API board

This example describes **how** the Hermes workflow was composed, not the product specification or a restorable board. It is not an instruction to modify an existing board or to release the API. No internal proposal, source document, live card, customer data, credential, IP, or project-specific approval exception is included.

- One project board uses reusable global role profiles: `planner`, `plan-reviewer`, `orchestrator`, `implementor`, `spec-reviewer`, `quality-security-reviewer`, `integrator`, and `e2e-tester`.
- A dedicated **project-invariant skill** is forced onto relevant project cards. Its source-of-truth requirements remain outside this public reusable template. Do not replace it with the generic skills or assume a new project inherits those rules.
- Main chat delegates neutral discovery to planner. Planner hands its hashed attached proposal to independent same-card plan review; human product approval follows before an orchestrator creates execution cards.
- Execution uses per-lane branches/worktrees and native same-card specification review followed by separate quality/security, prospective integration, exact-candidate E2E, verified `dev` promotion, and separately guarded cleanup. `main` requires a distinct explicit release instruction.
- Board-specific chat notifications are subscriptions, not permissions. Always specify the board on CLI/chat commands; the machine-wide default board might be different.

To reuse this pattern elsewhere, create a **new** board and a new project-invariant skill, choose a repository and human approval venue, verify the role/model routes in `profiles.json`, then perform the harmless review and dependency-gate tests described in the parent README. Do not import the Partner Network API board or copy its attachments as starter data.
