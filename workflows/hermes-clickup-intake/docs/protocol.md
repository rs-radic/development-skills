# Protocol and integration contracts

This is a standalone reference for the supplied worker templates and deterministic adapter. Numbered `§` references and S0–S4 names in the inherited source/tests refer to the original implementation phases; that private planning document is not required or included. The code, tests and contracts below define this public package.

## Hermes contracts

Run the real-CLI tests before using another Hermes build. The adapter requires:

- `hermes kanban --board <board> create --body-file ... --idempotency-key ... --created-by ... --initial-status blocked --assignee ... --skill ... --json`.
- `show --json` returning `task`, `runs`, `comments`, `parents` and review/completion metadata; `attachments --json` returning names, sizes, uploader and accessible `stored_path` values.
- `comment`, `attach`, `unblock`, `complete --metadata`, `archive`, `notify-subscribe` and `notify-list --json` with the semantics exercised by the tests.
- Worker `kanban_schedule(reason="CLICKUP_WAIT")`, leaving a `scheduled` run, and same-card `kanban_request_review` to `plan-reviewer`. Ordinary polling must not create `needs_input` blocks merely because it is waiting for a human response.
- Passive notifications (`delivery_mode="notify"`), a named notifier profile and inheritance of the orchestrator's subscriptions by its child cards.
- `hermes cron create ... --script ... --no-agent --paused` and normal pause/resume control.

The poller shells out to a configured executable; it does not patch Hermes or provide these APIs. The test harness is isolated, but uses the **actual** installed CLI. Missing metadata or different status semantics is a compatibility failure, not permission to invent a passing response.

The `default` profile is the notifier. `planner`, `plan-reviewer` and `orchestrator` are fixed role names. Worker models and credentials are not configured by this package. Configure the exact selected planning skills in each participating profile; naming a skill on a card does not install it.

## Intake and conversation

Only nonarchived, top-level tasks in the configured home list and intake statuses are discovered. The cutoff is inclusive as implemented by the observer's comparison; use a clearly defined activation time rather than relying on an exact-millisecond race. A null cutoff baselines observed tasks without automatically admitting them. `admit` explicitly opts an eligible task in.

The current request card is reused for its conversation; a later revision/amendment can create a successor request card. After writing a protocol output, the worker schedules itself as `CLICKUP_WAIT`. The poller relays settled, relevant input and unblocks that same parked run after readback; it does not create a new worker card for every comment.

Worker outputs use this **first line**, with concrete values instead of alternatives:

```text
CLICKUP-OUT v1 kind=business_questions|technical_questions|answer|notice|classification relay=<ref> [reply_to=<comment-id>] [gen=<G> result=answer_only|material]
```

The remaining text is the message. Ordinary worker commentary is not automatically posted to ClickUp. Incoming `CLICKUP-IN v1` relays refer to a context attachment and generation. The worker must classify each delivered relay as harmless conversation (`answer_only`) or scope change (`material`). Untrusted ClickUp text remains inside a nonce envelope and cannot alter these instructions or authorize effects.

Recognized human closure lines:

```text
Business requirements closed
Technical requirements closed
```

The requester or an allowlisted approver can close business requirements. Technical closure requires an approver. A single authorized person suffices; there is no quorum. Closures cause reevaluation, not development approval. Follow-up questions must actually mention the bot using a ClickUp user tag. Human activity restarts the quiet timer; the bot's own activity does not.

## Plan review and publication

The planner attaches a new file for every revision, beginning with:

```markdown
## ClickUp summary
A concise summary of the proposed work.
```

Use a summary of at most 2,000 characters and a new `clickup-plan-r<N>.md` name. Request independent review with a summary and metadata:

```text
kanban_request_review(
  reviewer="plan-reviewer",
  summary="Plan revision ready for independent review",
  metadata={
    "clickup_task": "<task-id>",
    "plan_revision": 1,
    "plan_attachment": "clickup-plan-r1.md",
    "plan_sha256": "<64 lowercase hex characters>",
    "accounts_gen": <highest input generation delivered before review>
  }
)
```

The adapter requires the completed final run to belong to `plan-reviewer`, the preceding review request to carry the metadata, and the stored attachment bytes to match the hash. It posts the plan only when the revision is still current. “Review requested” is not PASS; PASS is not development approval.

## Development approval

After publication, an allowlisted approver posts a **new, unedited, top-level plain-text comment**:

```text
Approve plan r1 for development
```

The parser normalizes whitespace/case and one optional final period, but does not infer approval from “yes,” a quoted line or a stale revision. Unknown rich-text attributes fail closed. `plain_attr_allowlist` is for separately verified harmless attribute keys; do not add quote/code attributes to make a failed approval appear valid.

### Plain-comment calibration

The October 14, 2026 source update added regression coverage for a ClickUp plain-editor representation equivalent to:

```json
[
  {"text": "Approve plan r1 for development", "attributes": {}},
  {"text": "\n", "attributes": {"block-id": "block-example"}}
]
```

Here `comment_text` is the command plus a newline. The sample installation settings use `"plain_attr_allowlist": ["block-id"]`, allowing this neutral metadata key without relaxing the parser. The runtime's default remains empty; an existing private config must opt into the calibrated key. `tools/prepare.py` preserves the supplied allowlist.

`tests/test_schema_f1.py` compares the empty baseline (unknown/rejected) with the calibrated shape (plain), including the composed closure-to-reviewed-approval flow. Quote/code markers remain quoted; unknown attributes/part types, malformed data and disagreement with `comment_text` remain unknown. Author, stage, edit, current-revision and post-publication checks still apply. This is not proof that every ClickUp UI or future editor format has been calibrated.

Approval is bound to the API actor ID, comment, plan revision, hash and input generation. The display name is explanatory only. Extra input can cause reclassification, amendment or revocation. The integration's readback/outbox controls prevent retries from becoming duplicated effects or an implicit approval of different bytes.

The adapter creates:

1. An unassigned, blocked approval gate owned by `clickup-intake`.
2. An orchestrator card with the exact plan and approval record, plus current context/ledger attachments.

The adopted project workflow must then create the implementation, native specification review, independent quality/security, candidate, final E2E, promotion and final cleanup stages. This package does not implement those workers' project logic.

## Orchestrator completion metadata

The orchestrator completes with concrete card IDs:

```json
{
  "clickup_gates": {
    "plan_revision": 1,
    "plan_sha256": "<64 lowercase hex characters>",
    "final_e2e": "t_<card-id>",
    "promotion": "t_<card-id>",
    "cleanup": "t_<card-id>",
    "lineage": ["t_<every-created-card-id>"]
  }
}
```

The promotion card must depend on the approval gate and include the exact marker and recheck paragraph supplied by `templates/ticket_recheck_paragraph.md`. Preserve the other project gates and its final resource-removal gate. A revised plan must safely replace superseded promotion/lineage while the old approval remains revoked.

On successful promotion, completion metadata must contain `promoted_sha`, a full 40-character Git commit SHA. This is how the adapter recognizes completed promotion, not by parsing a casual message. The adapter uses exact gate identities rather than guessing which project task is E2E or release.

## Promotion ticket

The marker is:

```text
CLICKUP-TICKET-CHECK v1 gate=<gate-card> task=<clickup-task> rev=<N> sha=<plan-sha256>
```

Immediately before the nonforced `dev` fast-forward, execute the installed entry's `ticket-check` using those four values. Only exit code **0** plus `VALID epoch=<N>` authorizes proceeding. Record that result with the governing-input evidence. Any other result blocks promotion and package delivery.

Validity is recomputed from local state plus a read of the matching gate. It requires the unique current approval to be ticketed, unrevoked, unsuperseded, current/classified input generations, a live lease, no hold/terminal task and gate status `done`. Gate comments are only an audit trail. An archived gate may release a Hermes dependency but **cannot** satisfy the ticket check.

Default renewal interval is 600 seconds and lease is 1,500 seconds. A stopped poller therefore cannot indefinitely authorize promotion. New input, a hold, missing state, wrong gate/hash/revision or expired lease fail closed. The ticket never grants permission to release `master`/`main`, deploy to production or enlarge project scope.

## Observation, transport and private state

The default scan budget is 40 requests, a 20-request remaining-rate-limit reserve and a 15-request share per task. Task discovery, comments and threaded replies use bounded pagination. Partial snapshots resume on subsequent scans rather than being presented as complete. Refresh intervals are defined in `config.py`; quiet time and scheduling cadence are separate.

ClickUp API credentials go only to `api.clickup.com`. Authenticated API redirects are refused. Attachment fetches carry no API token, use HTTPS and have a 10 MiB per-file / 20 MiB total cap. Failed/oversized/skipped files retain explanatory metadata. Review attachment access and project trust before activation; an HTTPS URL alone is not proof of safe content.

The SQLite store commits consumed inputs and pending effects together. The outbox checks current preconditions immediately before sends/retries and reconciles ambiguous outcomes against the external system. Operator abandon is not auto-resend. Failed or ambiguous effects may retain exact staging bytes for recovery. Keep the DB, WAL/SHM, logs, context and staging private; they can contain request data. Sanitizing source for publication does not sanitize operational data.
