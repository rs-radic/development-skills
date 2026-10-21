# ClickUp approval gate: task ${task_id}, plan r${rev}

Owner: clickup-intake (deterministic, no LLM). Approval of plan r${rev} (SHA-256 ${plan_sha}, accounts_gen ${accounts_gen}) by ${author_name} (ClickUp user ${author_id}), ClickUp comment ${comment_id}.

This card has no assignee and is never dispatched. The `dev`-promotion card of this plan has this gate as a parent. Completing or archiving this card releases `dev` promotion of r${rev}; it does not authorize it. Promotion still requires a valid ClickUp approval ticket: immediately before the non-force `dev` fast-forward the promotion card runs `ticket-check` and proceeds only on VALID. Ticket authority lives only in the integration's local state; `CLICKUP-GATE` comments on this card are an audit trail only and are never trusted.

${gate_line}

Do not complete, archive, unblock or comment on this card except as described above.
