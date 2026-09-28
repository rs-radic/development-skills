# Hermes skill routing by role and stage

This is a **placement guide**, not a dump of every skill installed in each profile. `profiles.json` records the **minimum reusable skill set** for each role; installation makes a skill available, while a card's `--skill`/`skills` field **force-loads it for that run**. Other installed skills are selected only when the task calls for them. Profile copies are independent: changing the default profile's copy does not update workers. Compare versions and content before replacing an existing skill.

## Four distinct delivery mechanisms

1. **Hermes runtime guidance (not a skill):** a dispatcher-owned Kanban task receives `KANBAN_GUIDANCE` in its system prompt **when that worker has the `kanban_show` tool available**. This supplies the basic task lifecycle; a disabled/unavailable Kanban toolset removes that guidance and the worker cannot follow this workflow. Verify `kanban_show` is available to each worker profile before dispatch. No `--skill kanban-worker` flag is needed to get the guidance under those conditions.
2. **Hermes native review:** when a card enters the native `review` lane, the dispatcher adds `sdlc-review` to its skills. This applies to both planner → plan-reviewer and implementor → spec-reviewer handoffs. An ordinary downstream quality/security card is **not** a native review-lane claim and does not gain `sdlc-review` automatically. Ensure the reviewer profile can load that installed skill.
3. **Reusable workflow skills from this folder:** install `approval-gated-kanban-development` and `plan-review-gate` for the profiles listed in `profiles.json`. Explicitly force `plan-review-gate` on new planning cards and `approval-gated-kanban-development` on execution cards. The planning skill stays on the same card when it moves to independent review.
4. **Per-project skills:** create and maintain a separate approved-invariant skill for each project; force it only onto cards whose planning, implementation, review, integration, or E2E verdict depends on those invariants. Put private requirements in that project skill or card source, **not** in this public reusable folder. A project skill does not change the generic human approval or `main` release gates.

Hermes removed the bundled `kanban-worker` and `kanban-orchestrator` skills in favor of injected guidance. Its installer/update changes the checkout but does **not** automatically remove separately copied skills from independent profile directories or rewrite skills pinned on existing cards. Audit those before retiring legacy copies; use the installed Kanban API for necessary edits and preserve running cards and historical evidence. New cards should not name either retired skill. `sdlc-review` remains dispatcher-forced on native review claims.

## Stage-by-stage placement

- **Main chat / owner:** load the reusable governance skill to route work; load the project's invariant/context skill when deciding scope or presenting a plan. Discuss and verify approval here; no automatic review or execution card is created by mentioning a skill. Use `hermes-agent` only for Hermes setup/troubleshooting, not as an always-on project skill.
- **Planner (`planner`, planning card):** force `plan-review-gate`; keep `approval-gated-kanban-development` available for scope and handoff rules; add the project-invariant skill when source requirements are needed. The card stays planning-only. `writing-plans` or domain research skills are optional for an appropriate plan, never implementation authority.
- **Independent plan reviewer (`plan-reviewer`, same card in native review):** `sdlc-review` is dispatcher-forced; `plan-review-gate` follows the card and must be loadable; add the same project-invariant skill when checking project requirements. The reviewer does not edit the plan or approve implementation. This role need not install the general development workflow skill merely to assess a plan.
- **Orchestrator (`orchestrator`, after exact owner approval):** force `approval-gated-kanban-development` plus the project skill where it constrains card creation; use the injected Kanban orchestrator guidance for lifecycle and mechanics. This role creates dependencies, not code, reviews, E2E, or a `main` release.
- **Implementor (`implementor`, isolated worktree):** force `approval-gated-kanban-development` plus applicable project skill. Load `test-driven-development` and `pragmatic-code` as needed for product code; `requesting-code-review` is useful for pre-commit verification, but independent Kanban reviewers still own the subsequent gates. At completion, request native spec review on the **same card**.
- **Specification reviewer (`spec-reviewer`, implementation card in native review):** dispatcher-forced `sdlc-review`, inherited workflow skill, and the applicable project-invariant skill. The reviewer checks spec and project rules without changing code and returns corrections to the implementor on the same card.
- **Quality/security reviewer (`quality-security-reviewer`, separate downstream card):** force the workflow and applicable project skill; optionally load `requesting-code-review` for an independent code/security lens. This ordinary card must not complete with a failed verdict, because completion releases integration; route remediation and repeat invalidated gates.
- **Integrator (`integrator`, candidate/promotion/cleanup cards):** force the workflow and relevant project skill. Use a Git/GitHub or conflict-resolution skill only when that operation actually occurs. Prepare a prospective candidate first; promote only the E2E-proven SHA to `dev`; local cleanup is a separate guarded card. No automatic `main` permission.
- **E2E tester (`e2e-tester`, exact candidate card):** force the workflow and applicable project skill; load `rest-graphql-debug` only for REST/GraphQL diagnostics, or a UI visual-QA skill only for UI evidence. These domain skills are optional tools, not a substitute for real exact-candidate E2E or an approval gate.

## Example card skill routing

```text
New planner card: --assignee planner --skill plan-review-gate --skill PROJECT_INVARIANT
Native plan review: same card -> plan-reviewer; Hermes adds sdlc-review
Approved orchestrator card: --assignee orchestrator --skill approval-gated-kanban-development --skill PROJECT_INVARIANT
Implementation card: --assignee implementor --skill approval-gated-kanban-development --skill PROJECT_INVARIANT
Native spec review: same implementation card -> spec-reviewer; Hermes adds sdlc-review
Quality/E2E/integration cards: --skill approval-gated-kanban-development --skill PROJECT_INVARIANT when applicable
```

Replace `PROJECT_INVARIANT` with a real installed skill name and keep the approved plan/source attached to the card. Do **not** force a private project skill onto a different project's board or assume every generic helper belongs on every card. Before dispatch, verify each assignee profile can load the exact skill versions named by its cards; `--skill` on a card does not install a missing skill into another profile. The worker profile's model route is independent of its skill list (see `profiles.json`).
