# ClickUp approval record: task ${task_id}, plan r${rev}

Recorded by clickup-intake (deterministic, no LLM) from ClickUp API user ids, never display names.

- Approver: ${author_name} (ClickUp user ${author_id}), one of the configured approvers.
- ClickUp comment ${comment_id}, posted ${comment_date}: a new, unedited plain-text comment whose whole text is "${approval_line}".
- Plan r${rev}: SHA-256 ${plan_sha}; accounts_gen ${accounts_gen}.
- Approval gate card: ${gate_id}.
- Authority boundary (D2, auto_gate=${auto_gate}): this approval carries the work through `dev` promotion and the production ZIP only; never `master`, never production deployment. `dev` promotion additionally requires a valid ClickUp approval ticket (`ticket-check` exits 0 with VALID immediately before the fast-forward).
