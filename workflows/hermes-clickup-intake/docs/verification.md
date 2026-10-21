# Publication boundary and verification

## Source lineage

This package was extracted from the host-side ClickUp intake at source commit `f24c844cd9833993359b46357affd9fdd7c5b4b0`, not from the managed application's repository. The source deployment used Windows and Hermes commit `1fd18277267152893a8a24792b79213e7ca3f899` (`rc.13-v0.21.7`). Its runtime was already installed before this public extraction.

The follow-up source revision `20dfb78e8c7e7e6d48530f1181f7b6d5778d05e1` adds `tests/test_schema_f1.py` for the observed neutral `block-id` editor attribute. Its deployed correction is configuration-only: `plain_attr_allowlist` contains `block-id`. The public sample now carries that calibration, with sanitized observation provenance and unchanged parser/runtime code. The original publication's 310-test result remains historical; follow-up checks are not presented as a new live ClickUp test.

The accepted refresh is source commit `542a3a0b5ab45434345493f11095260daa6edd51`, compared with the previous public source `20dfb78e8c7e7e6d48530f1181f7b6d5778d05e1`. It adds structured promotion-receipt compatibility, outbound-prose correction feedback, and the r12 human-decision question/answer, fence, park and activation/disable/rollback protocol. The source was accepted through specification, independent quality/security and isolated end-to-end checks before installation and activation. Those are source acceptance facts, not a new public-package test run or proof of a live human exchange; no live question/answer exchange was observed at cutover.

The next refresh pins source commit `aa3cbe67c99822399487bc5e3776ffac50aeb8c7`, whose rules module was also verified against the active immutable release. It adds a narrow plan-publication recovery path: a completed reviewer's explicit PASS and canonical artifact binding can validate the latest planner request's matching legacy metadata. It does not infer approval from prose, modify historical metadata or authorize development. This publication adds `tests/test_pass_handoff.py` unchanged from that source; its cases cover one-time publication, mismatched or missing bindings, invalid reviewer receipts and stale input generations. Earlier source-acceptance and test results remain historical, not a claim that this follow-up repeated live ClickUp or worker-model execution.

The technical-recovery refresh pins source commit `149042d7edbfd2b279f00164d66e685d30da8119`, compared with the previous public source `aa3cbe67c99822399487bc5e3776ffac50aeb8c7`. It adds separately enabled, approval-bound recovery incidents, operator-wake carriers, read-only admission, bounded ACK/retry/escalation handling, and disable/rollback checks. The source lane reported an isolated run of 628 tests with zero skips and 22 forced-exit cases. These are **reported source-isolation results**, not new public-package execution results. At this publication handoff the source entry selected the pinned revision, but live recovery acceptance was still in progress; this package does not claim a completed live rollout or a successful end-to-end operator exchange.

The source deployment's active entry selects an immutable release. The public package instead preserves the tracked flat-layout entry and uses offline preparation to produce a **new** disabled staging copy. Unversioned files beside a deployed loader are not necessarily active. Publication does not switch that loader, upgrade an existing installation or change its state. A preceding source-only template change for a private project's current-development policy is not imported into the generic public request/orchestrator templates.

The public copy preserves the polling, strict parsing, durable generation/approval state, outbox/recovery logic, ticket checks, API safeguards and internal protocol/idempotency names. Public adaptations are limited to:

- Synthetic values replacing actual people, IDs, machine paths and project identifiers in source defaults and regression fixtures.
- Generic project instructions replacing inaccessible private QA/release references. Public planner defaults use `plan` and `pragmatic-code`; the operator supplies their own additional project skills.
- A corrected template call example that includes the required review `summary` argument.
- A test-only executable override and path substitution in the isolated test copy, so real CLI tests can run against an explicitly selected Hermes installation.
- A sanitized rollback-baseline fixture and a local fixture-copy loader replacing the upstream test's private `git archive` dependency; the compatibility assertions are preserved.
- A second sanitized pre-recovery fixture, retaining all recovery activation/equivalence assertions without requiring private Git history. The recovery CLI source audit uses the previous **public** commit and package-relative paths, not an inaccessible private source commit.
- The recovery carrier's private board/path/contract references replaced with explicit operator-supplied project bindings. This is a publication adaptation, not automatic recovery authority or an installer for the referenced procedures.
- Offline installation preparation, its tests/example settings, this documentation and the repository index link.

No live configuration, credentials, state, original private plan or production data was copied. No new license or notice file is introduced. The public repository's existing terms apply; publication does not transfer third-party rights.

## What the tests exercise

The original regression suite covers foundational types/configuration/store, parsing and approval representations, ClickUp observation/effects using fake transport, planner/workflow generations, attachments, replay/retry, strict promotion tickets, and complete local composition. Real-Hermes CLI cases use temporary homes and boards, covering metadata, review, scheduling/unblocking, dependency gates, attachment readback and the scheduler-shaped no-argument entry.

The refresh extends this coverage:

- `tests/test_s2_workflow.py` and `tests/test_s4_composition.py`: canonical/structured receipt acceptance, disagreement/malformed receipt refusal, unchanged status on refusal, and bounded filter correction/hold recovery across restarts and delayed delivery.
- `tests/test_s3_kanban.py`: benign password prose versus credential-shaped values and generated promotion-card receipt requirements.
- `tests/test_question_relay.py`: question-card identity/triage, closure and generation-bound resolution, first delivery/correction, multiple answers, ACK/fence/park ordering, review-lane preservation, holds, terminal/superseded cases, ambiguous-effect readback and private-text rejection.
- `tests/test_question_release.py`: fence observation cadence, enable-dependent protocol rendering, idempotent adoption comments and controlled disable/drain behavior.
- `tests/test_activation.py`: locked feature commands, additive schema, rollback refusal conditions and old-reader compatibility on a populated candidate database.
- `tests/test_recovery.py`: actionable typed stops and exclusions, current-approval/lineage membership, event/reason/approval admission bindings, body bounds, carrier effects and stale/final-ACK send gates, recurrence/wake budgets, working ACKs, handoff tracking, adoption and disable behavior.
- `tests/test_recovery_activation.py`: locked recovery commands versus lock-free read-only admission, rollback refusal, prior-reader compatibility with the question relay enabled, and feature-off lifecycle equivalence.
- `tests/test_recovery_cli.py`: real local carrier creation/subscription/completion and silent archival, delayed/duplicate admission refusal with unchanged state, and the delegated-child mutation refusal plus source guard audit. These are not live notifier delivery or worker-model tests.

The rollback test's `BASELINE_SHA` remains `b665a985434e296f5b3fd796dc34357d0d6464ba` as provenance. Its **executable input** is the bundled `tests/fixtures/rollback_base/` tree (15 Python package modules and five templates), with the same public sanitization as the package. The loader copies that fixture instead of requiring private source Git history. It does not restore any installed runtime or database. Old-reader success on finalized relay rows is not evidence that a downgrade with pending relay work is safe.

The recovery compatibility fixture records source baseline `2d53206cb907ae7d1f2e3e8ca0ce0e5170f217fd` as provenance. Its executable input is `tests/fixtures/recovery_base/`: 15 package modules and seven templates with a verified fixture manifest. The recovery activation test copies those checked local bytes instead of running a private-history archive command. The earlier `rollback_base` fixture remains separate and unchanged. Neither fixture is an operational rollback tool.

`tests/test_public_prepare.py` adds offline preparation checks, including required `blocker_triage.md`, `question_protocol.md` and `recovery_card.md` templates. Missing recovery-template refusal is exercised before staging, while successful preparation verifies the 25-file payload and every manifest byte/hash. All earlier assertions remain in place. These are not live ClickUp tests. The preparation utility must not read a credential, contact an API, activate cron or overwrite an installation. Its output remains disabled/shadow and flat-layout, with no state database, question-relay activation or technical-recovery activation copied from another installation.

Tests are intended to run from this package directory with:

```powershell
$env:CLICKUP_INTAKE_TEST_HERMES_EXE = '<absolute path to the compatible hermes.exe>'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -v
```

The recovery CLI source audit requires a **full public Git clone** containing `532c102bdaa5bf2f7238f37973ce9e0563ddf0a9`, the prior public baseline. It compares that commit with the candidate's tracked and untracked files under this package, preserving the original forbidden-mutation assertions. A source ZIP or shallow clone without the baseline cannot run this audit. The bundled recovery fixture removes the other test's private-history dependency; it does not replace the public Git baseline needed by this source audit.

Use a clean, disposable testing context. Run all real-CLI cases serially: both the older CLI contracts and new recovery CLI cases share a fixed temporary home. To exercise the new cases separately, with the same executable override, run `python -B -m unittest discover -s tests -p test_recovery_cli.py -v` only when no other suite owns that home. A terminated test can leave its own `tests/.tmp` home; verify no associated test process is active before removing that exact fixture. Never run a broad cleanup over another Hermes installation.

Report public execution separately from source evidence. If older real-CLI tests are not rerun, name those exact test IDs as not run and bind any reused evidence to unchanged test code and relevant runtime contracts. A passing subset or an opt-in case that returned early is not a complete-suite pass. New recovery CLI cases require their own actual execution; their presence in the source lane's result is not a public-package test receipt.

## Limits that remain

- No claim of Linux/macOS support or arbitrary stock-Hermes compatibility.
- No OAuth enrollment, multi-tenant service, generic workflow engine, or turnkey worker provisioning.
- The operator must supply and approve a project workflow, its security boundaries, model credentials, repository permissions and release instructions.
- Public-source tests do not contact ClickUp, run real planning models or exercise actual browser-generated rich-text representations. Isolated question/answer and recovery scenarios do not prove live ClickUp-to-worker delivery, worker obedience to the fence/park or operator-recovery instructions, a real user's later answer, or a completed live recovery rollout. Installing/activating the source is not that proof. The originating deployment's live API tests were qualified by an explicit skip of UI quote and reply-with-quote checks and a contained test-configuration exception. Those are historical limitations, not proof or an inherited exemption for another installation.
- Source defaults and sample IDs are intentionally not a runnable live configuration. Generated private settings must be checked before activation.
- A passing ticket authorizes only the adopted `dev`/package path. Deployment and release-branch promotion remain separate owner decisions.

## Publication checks

Before publication, verify the complete candidate with the tests above, syntax checks, a relative-link check, a private-value/credential scan, and an independent source/portability review. Check the new templates are in the prepared manifest, all fixture paths/manifests resolve locally, and the real disabled no-argument entry remains silent without credentials or state creation. Render both relay-off and relay-on worker templates plus the recovery carrier, preserving the target project's policy and command bindings; confirm no inaccessible private board/contract references were retained. Keep the tested public candidate frozen during execution; do not run multiple fixed-path real-CLI suites concurrently. Bind that review to an exact staged tree. Read back the GitHub commit and published file bytes after pushing. Keep detailed source hashes and verification receipts outside the public package; do not publish internal review logs or task history.
