# Publication boundary and verification

## Source lineage

This package was extracted from the host-side ClickUp intake at source commit `f24c844cd9833993359b46357affd9fdd7c5b4b0`, not from the managed application's repository. The source deployment used Windows and Hermes commit `1fd18277267152893a8a24792b79213e7ca3f899` (`rc.13-v0.21.7`). Its runtime was already installed before this public extraction.

The follow-up source revision `20dfb78e8c7e7e6d48530f1181f7b6d5778d05e1` adds `tests/test_schema_f1.py` for the observed neutral `block-id` editor attribute. Its deployed correction is configuration-only: `plain_attr_allowlist` contains `block-id`. The public sample now carries that calibration, with sanitized observation provenance and unchanged parser/runtime code. The original publication's 310-test result remains historical; follow-up checks are not presented as a new live ClickUp test.

The accepted refresh is source commit `542a3a0b5ab45434345493f11095260daa6edd51`, compared with the previous public source `20dfb78e8c7e7e6d48530f1181f7b6d5778d05e1`. It adds structured promotion-receipt compatibility, outbound-prose correction feedback, and the r12 human-decision question/answer, fence, park and activation/disable/rollback protocol. The source was accepted through specification, independent quality/security and isolated end-to-end checks before installation and activation. Those are source acceptance facts, not a new public-package test run or proof of a live human exchange; no live question/answer exchange was observed at cutover.

The source deployment's active entry selects an immutable release. The public package instead preserves the tracked flat-layout entry and uses offline preparation to produce a **new** disabled staging copy. Unversioned files beside a deployed loader are not necessarily active. Publication does not switch that loader, upgrade an existing installation or change its state.

The public copy preserves the polling, strict parsing, durable generation/approval state, outbox/recovery logic, ticket checks, API safeguards and internal protocol/idempotency names. Public adaptations are limited to:

- Synthetic values replacing actual people, IDs, machine paths and project identifiers in source defaults and regression fixtures.
- Generic project instructions replacing inaccessible private QA/release references. Public planner defaults use `plan` and `pragmatic-code`; the operator supplies their own additional project skills.
- A corrected template call example that includes the required review `summary` argument.
- A test-only executable override and path substitution in the isolated test copy, so real CLI tests can run against an explicitly selected Hermes installation.
- A sanitized rollback-baseline fixture and a local fixture-copy loader replacing the upstream test's private `git archive` dependency; the compatibility assertions are preserved.
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

The rollback test's `BASELINE_SHA` remains `b665a985434e296f5b3fd796dc34357d0d6464ba` as provenance. Its **executable input** is the bundled `tests/fixtures/rollback_base/` tree (15 Python package modules and five templates), with the same public sanitization as the package. The loader copies that fixture instead of requiring private source Git history. It does not restore any installed runtime or database. Old-reader success on finalized relay rows is not evidence that a downgrade with pending relay work is safe.

`tests/test_public_prepare.py` adds offline preparation checks, including the required new `blocker_triage.md` and `question_protocol.md` templates. These are not live ClickUp tests. The preparation utility must not read a credential, contact an API, activate cron or overwrite an installation. Its output remains disabled/shadow and flat-layout, with no state database or question-relay activation copied from another installation.

Tests are intended to run from this package directory with:

```powershell
$env:CLICKUP_INTAKE_TEST_HERMES_EXE = '<absolute path to the compatible hermes.exe>'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -v
```

Use a clean, disposable testing context. A terminated test can leave its own `tests/.tmp` home; verify no associated test process is active before removing that exact fixture. Never run a broad cleanup over another Hermes installation.

## Limits that remain

- No claim of Linux/macOS support or arbitrary stock-Hermes compatibility.
- No OAuth enrollment, multi-tenant service, generic workflow engine, or turnkey worker provisioning.
- The operator must supply and approve a project workflow, its security boundaries, model credentials, repository permissions and release instructions.
- Public-source tests do not contact ClickUp, run real planning models or exercise actual browser-generated rich-text representations. Isolated question/answer scenarios do not prove live ClickUp-to-worker delivery, worker obedience to the fence/park instructions, or a real user's later answer. Installing/activating the source is not that proof. The originating deployment's live API tests were qualified by an explicit skip of UI quote and reply-with-quote checks and a contained test-configuration exception. Those are historical limitations, not proof or an inherited exemption for another installation.
- Source defaults and sample IDs are intentionally not a runnable live configuration. Generated private settings must be checked before activation.
- A passing ticket authorizes only the adopted `dev`/package path. Deployment and release-branch promotion remain separate owner decisions.

## Publication checks

Before publication, verify the complete candidate with the tests above, syntax checks, a relative-link check, a private-value/credential scan, and an independent source/portability review. Check the new templates are in the prepared manifest, all fixture paths resolve locally, and the real disabled no-argument entry remains silent without credentials or state creation. Render both relay-off and relay-on worker templates, preserving the target project's policy and ticket command bindings. Keep the tested public candidate frozen during execution; do not run multiple fixed-path real-CLI suites concurrently. Bind that review to an exact staged tree. Read back the GitHub commit and published file bytes after pushing. Keep detailed source hashes and verification receipts outside the public package; do not publish internal review logs or task history.
