# Approved amendment-answer continuation

This optional protocol carries an existing material question into a **newer reviewed and explicitly approved plan**. It preserves the same question/work lineage and normal answer/fence consumption. It is not an inferred approval, a second implementation graph, or authority to complete a question by hand.

## Separate activation

Prefix all commands with `<python_exe> <installed-entry>`:

```text
blockers amendment-status
blockers amendment-enable --drain-sha <64-lowercase-hex-SHA256>
```

`amendment-status` is lock-free and opens the existing state read-only; missing state returns exit 2. It reports binding states, amendment enablement, admission state and readiness for the installed fixed rollback target.

`amendment-enable` is a **writer** command. It requires open admission, an enabled question relay and a well-formed drain digest. The first successful call records `{ms, drain_sha256}` in `meta.amendment_cutover_ms`; later calls retain that first record. It does not read or validate the drain receipt's contents, grant approval, enable polling, or enable technical recovery. The operator must first establish the approved drain/cutover procedure and receipt. Preparation creates no database and supplies no activation record.

## Bind only the exact reviewed decision

The new plan may include an unformatted whole line:

```text
CLICKUP-INCORPORATES v1 blocker=<N> question=<question-card> card=<work-card> gen=<G>
```

Use real identifiers/generation from the existing question, never placeholders or a newly invented question. The reader scans only matching `clickup-plan-r<N>.md` source attachments whose actual bytes match the current reviewed/approved plan hash. It reads at most the first 20 valid markers in file order. Files over 1 MiB are not scanned for markers; oversized files and excess markers produce alerts, not implicit incorporation. Indented, quoted, partial or malformed lines are not markers.

A marker binds a recorded, undelivered `scope_change` blocker to a later approval only when question/work identities and generation match, the resolution is `authority=unchanged`, and the later approval accounts for that generation. If every resolution field is absent, the narrow approved-plan path may derive an incorporation notice from those exact reviewed plan bytes and the already-classified **material** relay. It cannot invent a missing human answer from ordinary prose, accept partially populated resolution metadata, or bind an unreviewed plan.

The binding progresses `bound` → `applied`, or becomes `void` with a durable reason. Application requires current classified input/authority, an eligible unheld/nonterminal task, a still-blocked question, and the same work card still `todo` behind that question, without an open or newer claimed run and within the new approval's declared cards. Unknown native reads defer; changed question/resolution/approval/input invalidates the binding. Temporary holds pause it rather than discarding valid evidence. Terminal tasks void pending bindings.

Applying a binding changes the local governing approval and allows the existing normal answer-delivery path. It does not itself mutate the native question or resume a worker. The ordinary answer fence, ACK, correction, sibling-question, ticket and security boundaries still apply. A marker is not an operator recovery command.

## Explicit historical adoption

Only an authorized operator with an exact reviewed receipt may use:

```text
blockers adopt-amendment --blocker <N> --approval <N> --receipt <absolute-path> --receipt-sha <SHA256>
```

This command takes the writer lock, reads that local receipt and verifies its SHA-256. The receipt must contain the exact whole line constructed from the live blocker, resolution and approval:

```text
CLICKUP-INCORPORATES v1 blocker=<N> question=<Q> card=<W> gen=<G> relay=<ref> resolution_sha256=<SHA256> task=<task> approval=<N> rev=<N> sha=<plan-SHA256>
```

It also requires relay/amendment enablement, open admission, an undelivered unchanged-authority `scope_change` resolution, the newer current approval, no task hold/terminal state, a blocked question and an eligible `todo` work card still behind it. It reads Kanban but does not load the ClickUp token or send an external write. Repeating an existing pair returns the existing binding; it is not a refresh or permission to replay it.

## Rollback admission is not an installer

```text
blockers rollback-prepare --target <installed-supported-target>
blockers admission-open
```

`rollback-prepare` accepts only `main.ROLLBACK_TARGET`, a fixed source constant, not an arbitrary revision. It checks for bound amendment rows, applied rows whose blockers are not final, and any sending/ambiguous outbox rows. If ready, it **atomically closes admission** in the same transaction. It neither switches source nor restores a database. A separate recovery drain is required when a target predates recovery, and the question-relay rollback gate is still separate.

The public package's built-in target is `45f7c252df27`, the public release corresponding to source `149042d7edbf`. Its test-only `amendment_base` fixture records both full revisions and exact file hashes. This maps the deployment binding to an accessible public revision; it does not perform a checkout or authorize an operational rollback. Inspect the prepared constant and use an independently qualified rollback artifact. The preparer does not accept a hidden target override.

While admission is closed, ordinary polling and most writer commands stop. Read-only `status`, `ticket-check`, `recovery-check` and `amendment-status` remain outside the lock; the lock-taking exemptions are `blockers rollback-preflight`, `recovery status`, `recovery rollback-check` and `blockers admission-open`. `admission-open` deletes the local gate; it does not grant development, resume a worker or repair a release. Neither command removes the amendment activation record.

## Source map

- `rules.parse_incorporates`, `rules.adoption_line`: exact marker/receipt grammar.
- `main._observed_attachment`: file-name, size and marker projection.
- `engine._amend_ingest`, `_approved_plan_resolution`, `_amend_apply`: reviewed binding and native continuation predicates.
- `main.amendment_enable`, `adopt_amendment`, `amendment_status`, `rollback_readiness`, `rollback_prepare`, `admission_open`: command behavior and authority limits.
- `tests/test_amendment*.py`: marker, generation, adoption, activation, source-entry, template and rollback coverage. Test presence is not a claim that the current public suite passed; see [verification](verification.md).
