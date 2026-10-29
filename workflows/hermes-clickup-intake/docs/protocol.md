# Protocol and integration contracts

This is a standalone reference for the supplied worker templates and deterministic adapter. Numbered `§` references and S0–S4 names in the inherited source/tests refer to the original implementation phases; that private planning document is not required or included. The code, tests and contracts below define this public package.

## Hermes contracts

Run the real-CLI tests before using another Hermes build. The adapter requires:

- `hermes kanban --board <board> create --body-file ... --idempotency-key ... --created-by ... --initial-status blocked --assignee ... --skill ... --json`.
- `show --json` returning `task`, `runs`, `comments`, `parents` and review/completion metadata; `attachments --json` returning names, sizes, uploader and accessible `stored_path` values.
- `comment`, `attach`, `unblock`, `complete --metadata`, `archive`, `notify-subscribe` and `notify-list --json` with the semantics exercised by the tests.
- Worker `kanban_schedule(reason="CLICKUP_WAIT")`, leaving a `scheduled` run, and same-card `kanban_request_review` to `plan-reviewer`. Ordinary polling must not create `needs_input` blocks merely because it is waiting for a human response.
- Passive notifications (`delivery_mode="notify"`), a named notifier profile and inheritance of the orchestrator's subscriptions by its child cards.
- For the optional question relay: worker `kanban_create` with `workspace_kind="scratch"`, `kanban_link`, dependency `kanban_block`, `kanban_show` and `kanban_heartbeat`. `show --json` must expose `parents`, `children`, `latest_summary`, task creation/status/assignee fields, runs with IDs and start/end times, and creation/claim/dependency-wait events with their creator/run/lane metadata. These are identity and consumption checks, not optional display fields. Native dependency waits must preserve the work/review lane when a parent completes.
- For optional technical recovery: typed `blocked`/`block_loop_detected` events tied to claimed, ended runs; exact event timestamps/reasons and current worker identity; an unassigned blocked carrier that is never dispatched; `notify-subscribe --delivery-mode wake` plus subscription readback; and completion notifications that wake the authorized non-worker coordinator. Archive must not generate the completion wake. The operator's state-appropriate native resume path must be separately verified; the package does not add a worker mutation bypass.
- For stalled and pre-worker recovery: native dependency-wait events, full parent/attachment observations, live process-generation and heartbeat evidence, plus installed `hermes_cli.kanban_db.kanban_db_path`, update-lock and venv-completion helpers. `KanbanCli._preworker_snapshot` locates these modules from the configured executable's `<root>/venv/Scripts/hermes.exe` layout; it does not execute a made-up snapshot CLI. It refuses a custom child environment, an in-progress update or changed native read-contract files. The read-only SQLite projection must match the installed native schema/serializer, including JSON, scalar and NULL fields. Missing capabilities yield unavailable evidence, not empty successful snapshots.
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

The canonical path requires the request card to be done, its final run to belong to `plan-reviewer`, the latest review request to carry the typed metadata above, and the stored attachment bytes to match the hash.

If the planner's latest request lacks a valid canonical binding, the adapter can recover it from the final reviewer's **completed** run, but only with explicit `review_outcome="PASS"`, matching `reviewed_sha256`, and the full canonical binding above. That fallback also requires:

- The latest review request was made by `planner` and identifies the same attachment and hash through `plan_attachment`/`attachment` and `plan_sha256`/`sha256`. Every present canonical or legacy value must agree; neither field group may be absent.
- Its revision matches the reviewer's positive integer revision, either as that integer or the exact legacy `rN` string. Any task ID or input generation present in the request must also agree.
- The bound attachment remains available under the exact name and hash.

This is a narrow metadata-compatibility path, not prose-based PASS detection or permission to rewrite past run metadata. The adapter still posts the plan only when the revision and input generation are current. “Review requested” is not PASS; PASS is not development approval.

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

### Promotion receipt compatibility

New promotion cards must still report top-level `promoted_sha` after the nonforced `dev` fast-forward and remote SHA readback. The adapter requires the named promotion card to be `done` and inspects its latest `completed` run's metadata; an archived card or a narrative summary is not a receipt.

The accepted source adds a compatibility path for an older structured receipt:

```json
{
  "candidate": {"sha": "0000000000000000000000000000000000000000"},
  "promoted": {"dev_after": "0000000000000000000000000000000000000000"}
}
```

The zeros above are synthetic placeholders, not a commit to promote. Without a top-level `promoted_sha` key, both nested objects must exist and `candidate.sha` must be a full **40-character lowercase hex** value equal to `promoted.dev_after`. Either field alone, unequal values, short/uppercase hashes and prose-only claims fail closed: status stays unchanged and an operator alert identifies the missing valid receipt.

If the top-level key is present, it governs: an invalid/null value cannot fall back to the nested receipt. A non-null `promoted.dev_after`, when present in an object, must agree with it. `candidate.sha` need not equal a valid canonical SHA (a release commit may be above the candidate). This compatibility reader does not waive the preceding ticket check or any project gate, and does not ask new workers to omit `promoted_sha`. The adapter uses exact gate identities rather than guessing which project task is E2E or release.

## Promotion ticket

The marker is:

```text
CLICKUP-TICKET-CHECK v1 gate=<gate-card> task=<clickup-task> rev=<N> sha=<plan-sha256>
```

Immediately before the nonforced `dev` fast-forward, execute the installed entry's `ticket-check` using those four values. Only exit code **0** plus `VALID epoch=<N>` authorizes proceeding. Record that result with the governing-input evidence. Any other result blocks promotion and package delivery.

Validity is recomputed from local state plus a read of the matching gate. It requires the unique current approval to be ticketed, unrevoked, unsuperseded, current/classified input generations, a live lease, no hold/terminal task and gate status `done`. Gate comments are only an audit trail. An archived gate may release a Hermes dependency but **cannot** satisfy the ticket check.

Default renewal interval is 600 seconds and lease is 1,500 seconds. A stopped poller therefore cannot indefinitely authorize promotion. New input, a hold, missing state, wrong gate/hash/revision or expired lease fail closed. The ticket never grants permission to release `master`/`main`, deploy to production or enlarge project scope.

For the exact typed `lease_expired` stop of the current promotion card, normal polling can queue one idempotent same-card retry after renewal. It rechecks the stopped run, marker, approval/ticket identity, all native parents and the lease after those reads, then reads back the native unblock. Other invalid-ticket reasons are not auto-retried. Missing/slow reads defer; changed authority cancels; an operator abandonment remains abandoned. The resumed worker must still execute `ticket-check` immediately before promotion. This path is part of ticket handling, not activation of the optional recovery feature.

## Outbound content policy

The general `OUTBOUND-PROSE-F1` content filter is **retired**. `render.check_outbound` remains as a compatibility hook but returns no rejection, including for configured-token matches and credential-shaped example text. New ordinary planner posts and reviewed plan attachments do not enter the former automatic filter-feedback/hold loop. Historical `CLICKUP-FILTER` rows and compatibility helpers can still exist; their presence is not an active general secret filter.

Workers must still keep real credentials and private operational data out of posts. This change is not a redaction guarantee and does not remove destination authorization, API host/redirect restrictions, attachment integrity, comment-size handling, generation/approval checks or the outbox. The specialized question/resolution checks below still reject length, paths, IPv4 and non-ClickUp URLs; diagnostic question projections additionally reject their explicit `sk-` pattern. Do not describe these narrower protocol checks as general outbound filtering.

`outbox resend <attachment-effect-id>` has a narrow recovery for a publication stopped by the old policy: the root must be the never-sent `failed/FILTERED` attachment and its comment/status descendants `cancelled_stale/DEPENDENCY`, with zero attempts, no send/child/result state, no transport mirrors, identical preconditions and the original ordered dependency chain. The eligible, unheld (or filter-held), nonterminal task must still have the same classified generation and no published revision. Only then are those three existing rows rearmed and the old filter hold cleared atomically. Payloads, reviewed hash, IDs and accounting are retained. The normal live send-time guards still decide publication; no re-review, replacement graph, automatic development approval or blanket historical replay is implied.

## Human-decision question relay

This optional relay is the accepted **r12** question/fence/park protocol, using `v1` wire identifiers. It is off until `blockers enable` stores `blocker_cutover_ms` in the integration database's `meta` table. This is distinct from `config.json`'s intake cutoff and polling `enabled` flag. `blockers enable` writes a fresh cutoff on every invocation. No historical question card is backfilled.

While enabled, the integration watches the current ClickUp-origin orchestrator, final-E2E, promotion, cleanup and declared lineage cards, never the approval gate. Detection requires a fresh, initially blocked, never-run protocol parent assigned to `clickup-intake`, created by the waiting work card at/after the cutoff, with that work card as its sole child. Ordinary `needs_input` or `capability` blocks are not forwarded to ClickUp. Held tasks are paused, not discarded; missing card reads defer decisions rather than prove completion.

### Ask, triage and answer

The complete worker instructions are in [question_protocol.md](../templates/question_protocol.md); use them verbatim on every descendant and amendment card when the feature is enabled.

1. A worker needing a human business/technical decision creates a scratch card titled `CLICKUP-QUESTION v1 <work-card-id>`, assigned to `clickup-intake`, initially `blocked`. Its body explains the missing decision and options, without secrets, logs, paths, hosts or customer data. It links the question as parent of **its own** card, calls `kanban_block(kind="dependency", reason="CLICKUP-QUESTION <question-card-id>")`, then stops. It does not complete/unblock the parent itself.
2. A `CLICKUP-BLOCKER v1` relay sends that internal question to the planner request card with current context and [blocker_triage.md](../templates/blocker_triage.md). The planner may inspect the work card but must not mutate it. It classifies each blocker into a focused business/technical question or `blocker_internal`. Infrastructure/operator work or an answer already supplied by existing authority stays internal and raises an operator alert; the raw worker body is never quoted to ClickUp.
3. Technical questions use `boundary=none|security`; a missing/invalid value is treated as security. A boundary question must state that the boundary remains in force and that new authority requires a revised plan and approval. The question temporarily moves the visible ClickUp status to the relevant requirements stage without changing the underlying development phase.
4. The existing closure rules apply to fresh plain comments after the posted question: requester or approver for business, approver for technical. A matching closure can close multiple already-posted questions of that stage. Top-level comments or threaded replies may carry the closure; replies back to a threaded closure use its root. This still does not authorize development or a scope change.
5. The planner classifies the delivered input generation and gives each carried blocker a resolution (at most **1,600 characters**) or asks a changed, focused question if the answer is insufficient. An unchanged question is not reposted. The exact resolution header is:
   ```text
   CLICKUP-OUT v1 kind=resolution blocker=<id> relay=<ref> gen=<G> authority=unchanged
   ```
   Resolution text cites the answering ClickUp comments and says no security/containment control is waived. It must match this task, request card, delivered relay and newer generation. The generation must settle as `answer_only`, all input must be classified, and the approval must remain current before delivery. A material change follows the revision/reapproval path instead.
6. The first delivery completes the question card with a `CLICKUP-ANSWER v1` summary. Before confirmed consumption, changed answers are `clickup-intake` comments on the work card, with a higher `gen` and `supersedes=g<N>`. The highest generation for each question governs. Whole rendered answer/correction text is capped at **2,000 characters**; longer text is not delivered and raises an alert. The consume-time question limit is **4,000 characters**; IPv4 addresses, Windows/UNC paths and non-ClickUp URLs are rejected. The retired general outbound credential patterns are not applied. These checks supplement, not replace, the planner's stricter no-private-data instructions.

“Answer recorded” confirms a settled delivery, not that a worker has consumed it. The intake does not infer consumption from a new claim or directly unblock the watched work card; completing its question dependency permits native scheduling. A valid worker ACK produces the separate “resumed with your answer” notice. Invalid/slipped ACK evidence or completion without an ACK can yield an unconfirmed-resumption notice and operator alert. After confirmed consumption, later input uses the ordinary task/revision path rather than retroactively replacing that consumed answer.

### Answer fence and bounded park

At run start, when a done question parent has no corresponding `CLICKUP-ANSWER-ACK v1` comment, the worker must **not read or act on the answer yet**:

1. Create `CLICKUP-FENCE v1 <work-card-id>` as a blocked scratch parent assigned to `clickup-intake`, link it to its own card, and wait for it to become done. Check about once per minute and heartbeat while waiting. The integration waits for classified input, settled relevant answers and the fence's observation barrier; all relevant answers are considered, not just the first question.
2. If still waiting after **30 minutes**, create and link a blocked `CLICKUP-PARK v1 <work-card-id>` scratch parent, then `kanban_block(kind="dependency", reason="CLICKUP-PARK <park-card-id> fence <fence-card-id>")` and stop. Do this even if the fence completes during park creation. The integration completes the park only after fresh reads confirm its identity/link and that the work card has **no current run**. A later run starts the fence step again. If park creation/linking fails, report an infrastructure `needs_input` block instead of dependency-blocking without a valid parent.
3. After a fence becomes done, read the work card, select the highest-generation trusted answer for each question, and post an ACK with this exact whole first line before continuing:
   ```text
   CLICKUP-ANSWER-ACK v1 question=<question-card-id> card=<work-card-id> gen=<G> run=<current-run-id> fence=<fence-card-id>
   ```

A valid ACK is bound to the question/work identities, a later claimed run in the original work/review lane, a delivered generation and an intake-released fence created during that run. Possibly-sent fence releases are reconciled before ACK disposition. Workers must never complete, unblock, unlink or comment on a question/fence/park card or link one they did not create. New approvals, security exceptions and production access are outside this protocol.

An open/releasing **fence** adds the task to the live observation cadence (light pass each scan, full pass at the configured `promotion_full_seconds`, default 300 seconds). A newly discovered eligible fence/park can release in its first complete scan; a light-only pass is insufficient. If the fence's rounded-up native creation anchor is newer than scan coverage, the poller can perform one bounded target-local full observation followed by a clean light confirmation, sharing the existing scan/task budget. Activity, partial pagination, missing native evidence or an unmet quiet barrier defers release; timestamps are never forged to make it current. A park still requires no current worker run. A park alone does not add ClickUp polling. This is not a wall-clock resumption guarantee. The 30-minute timeout is a worker instruction, not an independent timer service. Material input does not retract an already delivered parent result: the worker may resume with that earlier answer, while the scope notice, revised-plan approval and promotion-ticket checks still govern.

### Disable and rollback

`blockers disable` removes the relay cutoff, stopping new protocol-parent detection and blocker-relay staging. It cancels then-pending/retry-wait blocker asks and corrections except queued first-answer completions, preserves possibly-sent work for readback, and queues barrier-free releases for open/releasing fences. Recorded parks keep their no-current-run release condition. Per-question alerts identify unanswered questions or delivered answers whose later corrections/resumption are no longer relayed. Normal planning, approval, promotion polling and passive notices are not disabled. The command does not terminate already-running planner/work-card runs; reconcile their outputs as part of shutdown.

Allow the compatible poller to drain the retained effects before code rollback. With relay handling off, an authorized operator answers an open question by completing it with the decision as its summary, puts a later correction on the work card, or re-enables the relay. An operator may complete a fence; **complete a park only after confirming the work card has no current run**. These are maintenance actions within existing authority, not a means to grant scope or bypass tickets.

`blockers rollback-preflight` returns exit 0 and `rollback preflight OK` only when the relay is off, all `:blocker:`/`:bq:` outbox rows (including mirrors) are `done` or `cancelled_stale`, no blocker is `releasing`, and no fence/park is `open` or `releasing`. Failed/held-ambiguous rows also refuse rollback. The predicate does not drain work, change cards or validate unrelated outbox rows; the CLI takes the lock and opens the writer, which can create missing tables before checking.

The base database schema version remains **1**. Writer entry points add `blockers` and `fences` with `CREATE TABLE IF NOT EXISTS`; read-only ticket checks do not. Ordinary persisted preconditions retain their original nine keys; only blocker preconditions add `blocker_id` and `question_card`. The supplied rollback fixture exercises an older reader after the relay's pending work is settled. This is compatibility evidence, not permission to ignore preflight, remove tables, overwrite active state or assume all older releases are compatible. The public preparer produces a new flat-layout staging copy; existing immutable-release loaders and root bindings require a separately reviewed upgrade procedure.

## Technical-blocker recovery

Technical recovery is independent of the human-decision relay and remains off until `recovery enable` writes its own `meta.recovery_cutover_ms`. It covers exact typed worker stops, separately qualified stalled transitions (including an explicit no-worker case), and pre-worker approval staging. Ordinary queues, live writers, due retries and unresolved human dependencies do not qualify merely because they look idle. Diagnostic grace is ten minutes of unchanged qualifying observations, not a universal worker-start SLA. A staging anchor is `approval-<id>` with run 0, not a Kanban card. The operator must supply an approved project recovery contract before activation; no private contract or blanket continuation authority is included.

Each bounded attempt creates/subscribes/completes a separate intake-owned carrier, with a fresh current-incident/approval check before the wake. The carrier wakes the non-worker coordinator, not W; ordinary progress remains passive. The poller does not perform the operator's archive/attachment/resume operations. The coordinator must run the read-only `recovery-check` on wake, before every board write and again before resumption: exit 0 `CURRENT` is only admission, exit 3 `STALE` requires no board write, and exit 2 `UNAVAILABLE` requires a stop without ACK. Worker guards, exact approved targets/bytes, ticket checks and scope boundaries remain binding.

The [recovery contract](recovery.md) specifies eligible lineage, carriers and ACK formats, three-attempt/24-hour limits, genuine human-question readback and reply binding, current-authority operator limits, and separate disable/drain/rollback checks. A `cannot_act` ACK, a canned technical notice, a new assignment or a dead claim is not successful recovery or a verified human wait. Amendment bindings have their own [enablement and rollback contract](amendments.md).

## Observation, transport and private state

The default scan budget is 40 requests, a 20-request remaining-rate-limit reserve and a 15-request share per task. Task discovery, comments and threaded replies use bounded pagination. Partial snapshots resume on subsequent scans rather than being presented as complete. Refresh intervals are defined in `config.py`; quiet time and scheduling cadence are separate.

ClickUp API credentials go only to `api.clickup.com`. Authenticated API redirects are refused. Attachment fetches carry no API token, use HTTPS and have a 10 MiB per-file / 20 MiB total cap. Failed/oversized/skipped files retain explanatory metadata. Review attachment access and project trust before activation; an HTTPS URL alone is not proof of safe content.

The SQLite store commits consumed inputs and pending effects together. The outbox checks current preconditions immediately before sends/retries and reconciles ambiguous outcomes against the external system. Operator abandon is not auto-resend. Failed or ambiguous effects may retain exact staging bytes for recovery. Keep the DB, WAL/SHM, logs, context and staging private; they can contain request data. Sanitizing source for publication does not sanitize operational data.
