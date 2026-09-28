# Hermes Agent: approval-gated Kanban development

**Hermes Agent only.** This is not a generic Kanban board, an OpenClaw template, or an installable profile distribution. It documents a reusable setup for Hermes profiles, Kanban boards, native same-card reviews, forced skills, and a human-controlled release gate. The files in this folder are templates and a verified configuration snapshot; they do not modify a running Hermes installation.

## Components and authority

- A **board** isolates project cards, dependencies, evidence, and workspaces. A profile is a reusable role across boards; a project-specific skill supplies only that project's approved invariants. A chat subscription delivers status but does not approve work.
- **Planning:** main chat creates/reuses a neutral planner card; `planner` selects scope and attaches a versioned, hashed plan (or evidenced no-work finding). On the **same card**, `planner` calls `kanban_request_review(reviewer="plan-reviewer")`. The independent `plan-reviewer` either returns changes on that card or completes it with PASS. PASS means ready for human review, not authorization to implement.
- **Approval:** an authenticated owner approves the exact current artifact and resolves product decisions. Record approver, message/source, revision chain, attachment hashes, board, repository, target base, and any project-specific venue rule. An approval of a plan is not a release approval.
- **Delivery:** only after approval, `orchestrator` creates and verifies an idempotent card graph. For each lane: implementation + native same-card `spec-reviewer` handoff → separate quality/security review → prospective integration candidate → E2E of the **exact candidate SHA** → verified `dev` promotion → separate guarded local worktree cleanup. Remediation returns to the original implementor and invalidated gates rerun; neither failed E2E nor reviewer-requested changes advances `dev`.
- **Release:** never make automatic cards that write `main`. A fresh, verified owner instruction is required for a release; `dev` promotion, board completion, and planning approval are not that instruction.

## Files

- [`profiles.json`](profiles.json): live-observed role/model/provider/reasoning snapshot and **minimum reusable** skill set; installation does not automatically force a skill onto every card.
- [`skill-routing.md`](skill-routing.md): explicit role-and-stage map of Hermes runtime guidance, native-review skill loading, reusable workflow skills, optional helper skills, and project-specific invariant skills.
- [`skills/approval-gated-kanban-development/SKILL.md`](skills/approval-gated-kanban-development/SKILL.md): concise generic delivery procedure.
- [`skills/plan-review-gate/SKILL.md`](skills/plan-review-gate/SKILL.md): reusable independent planning review procedure.
- [`templates/planner-card.md`](templates/planner-card.md) and [`templates/orchestrator-card.md`](templates/orchestrator-card.md): required card fields and approval boundary.
- [`examples/partner-network-api/README.md`](examples/partner-network-api/README.md): **sanitized** project wiring example. No project requirements, source plans, customer data, credentials, or live board export.
- [`scripts/validate.py`](scripts/validate.py): offline structural and public-content checks for this folder.

## Reproduce on a new Hermes installation

1. Check the installed CLI (`hermes --version`, `hermes kanban --help`, `hermes profile create --help`). Create *new* profiles for the eight role names in `profiles.json` with `hermes profile create <name> --description "<role capability>"`; do not replace an existing profile or clone a live `.env`, auth store, sessions, or memory. Configure each new profile's `model.default`, `model.provider`, and `agent.reasoning_effort` from the manifest, then probe the saved route and verify the provider's canonical model. Authentication is provisioned independently, outside Git. Keep the reviewer identities distinct from authors.
2. Install the two reusable workflow skills into the default profile and **each profile that uses them**, preserving `SKILL.md` paths. Check the profile's actual skills rather than assuming default-profile edits propagate. For dispatcher-owned tasks, Hermes injects `KANBAN_GUIDANCE` **only when that worker can access `kanban_show`**; verify Kanban tools are enabled for each worker profile. The native review dispatcher adds the shipped `sdlc-review` skill on review claims. Hermes removed the bundled `kanban-worker` and `kanban-orchestrator` skills; do **not** install or force-load retained copies. The [skill routing guide](skill-routing.md) distinguishes runtime guidance, native review, reusable workflow, and project skills. If an installed skill of the same name already exists, compare it before replacing anything. Changes to live profiles are a separate operational decision, not a side effect of cloning this folder.
3. Create a project board explicitly: `hermes kanban boards create <board-slug> --name "<name>" --default-workdir "<absolute-repo-path>"` (do **not** add `--switch`). Use `hermes kanban --board <board-slug> ...` or explicit `/kanban --board <board-slug> ...` on every operation. Use a project-specific skill for approved product invariants and attach/force it on relevant cards. Keep a dedicated project context out of the generic template.
4. Check that the gateway dispatcher is actually running (`hermes gateway status`); start it only on a fresh installation with the operator's authorization. Confirm global settings before changing them: `kanban.dispatch_in_gateway=true`, `kanban.review_dispatch=true`, `kanban.auto_subscribe_on_create=true`, `kanban.notify_in_gateway=true`, `gateway.multiplex_profiles=true`. **Omit `kanban.dispatch_profiles` entirely** for unrestricted profile dispatch: an explicitly present `null`/empty value is fail-closed and claims no cards. Leave `max_in_progress` and `orchestrator_profile` unrestricted unless a cross-board effect is intended. `kanban.auto_decompose=false` was observed for the source installation but is **global**, not a project-only requirement. Do not change it solely to set up one board; bypass triage with explicitly assigned planning cards.
5. Run `python scripts/validate.py` from this folder. On a **separate throwaway board/repository**, verify a harmless planner → native plan-reviewer handoff, then a no-op implementation → native spec review → quality → candidate → E2E → promotion dependency graph **without any Git promotion or external writes**. Check assignees, effective model routes, workspaces, parent gates, source-chat notifications, and that no card can write `main`. Only then dispatch real work.

For a context-free planning trigger, explicitly create a card with `hermes kanban --board <slug> create "<title>" --assignee planner --workspace scratch --skill plan-review-gate --body-file <file>`; natural-language discussion by itself is not a deterministic card command. **CLI-created cards do not automatically inherit the originating chat's subscription.** Subscribe and read it back explicitly (replace placeholders, including the actual chat type and guild where applicable):

```text
hermes kanban --board <slug> notify-subscribe <task-id> --platform discord --chat-id <chat-id> --chat-type group --guild-id <guild-id> --notifier-profile <profile-serving-chat> --delivery-mode notify+wake
hermes kanban --board <slug> notify-list <task-id> --json
```

When creating from the originating gateway chat, verify the resulting subscription instead of assuming it. In the throwaway test, also verify a notification actually reaches that chat; a saved subscription row alone does not prove delivery. The `orchestrator` is created only after the exact reviewed plan has separate human approval.

## What is deliberately not exported

No live `kanban.db`, history, logs, workspaces, session transcripts, API keys, provider tokens, `.env`, full profile directories, project source documents, or customer data. A board export is a separate **sensitive data** operation, not an installation template. This repository folder does not change the Partner Network API board, its ongoing cards, or any profile. See the installed Hermes documentation and actual `--help` output for version-dependent commands.
