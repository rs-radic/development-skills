# ClickUp-origin plan r${rev}: task ${task_id}

Owner of this card: clickup-intake (deterministic, no LLM). This is an ordinary configured project orchestration of an owner-approved plan; the approval came from ClickUp.

## Governing inputs (attached to this card; read in full and verify byte size and SHA-256 before any effect)

- Plan: `${plan_name}`, ${plan_size} bytes, SHA-256 ${plan_sha}
- Owner-approval record: `clickup-approval-r${rev}.md` (it is the owner-approval note for this ClickUp-origin plan)
- Approval: ${author_name} (ClickUp user ${author_id}), ClickUp task ${task_id}, comment ${comment_id}, accounts_gen ${accounts_gen}
- Approval gate card: ${gate_id} (owned by clickup-intake; blocked; never dispatched)

## Requirements

1. Build the existing approved configured project graph (the adopted project workflow): implementation with native SPEC -> quality/security -> candidate -> exact-candidate E2E -> `dev` promotion under the adopted project release instructions -> guarded final cleanup; never `master`, never production deployment.
2. D2 boundary: ${d2_line}
3. Create the `dev`-promotion card with `parents` including the gate ${gate_id}, so it cannot start before the gate is released.
4. The `dev`-promotion card body must include, verbatim, the marker line and ticket recheck paragraph below (and may include the command line):

${promotion_block}

5. Complete this card with completion metadata `clickup_gates` naming the exact cards; only these ids drive the ClickUp status, so they must be exact:
   `{"plan_revision": ${rev}, "plan_sha256": "${plan_sha}", "final_e2e": "<final exact-candidate E2E card id>", "promotion": "<dev-promotion card id>", "cleanup": "<final cleanup card id>", "lineage": ["<every card id you created for this plan>"]}`
6. D3 Discord visibility: status events of this ClickUp-origin graph go to the configured Discord channel as passive `notify` subscriptions only; cards you create inherit this card's passive subscription. Never add `wake` or `notify+wake` subscriptions for this graph; Discord replies are never required.
7. No ClickUp contact and no ClickUp token for any worker; the integration relays status to ClickUp itself.
${amendment_block}