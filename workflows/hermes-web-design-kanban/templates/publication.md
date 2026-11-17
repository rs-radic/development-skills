# Final exact-revision publication

Follow the [workflow](../README.md). Stay in `technical approval` through publication, CI, production verification and physical cleanup.

## Request final approval

- Same task and canonical project authority: `<references>`.
- Exact completed concept / reviewed source / shared DEV revision and artifact bindings: `<identities>`.
- Independent SPEC and quality/security PASS plus actual shared DEV deployment and independent E2E PASS: `<bound-evidence>`.
- Tested DEV URL, results, limitations and unresolved failures: `<evidence>`.
- Exact task delta and accepted dependencies (not the whole shared DEV tip): `<scope-and-commits>`.
- Protected release branch, existing CI/deployment contract and authorized publisher: `<operator-bindings>`.
- Separately configured release approver set and designated approval surface: `<private-identity-bindings>`.

**Human authority record, separate from technical gates:** actual platform author, source reference, explicit release action, exact tested revision/scope and freshness: `<approval-evidence>`. One eligible release approver suffices. Display names, quotes, forwarded text, bot comments and status moves do not approve. Concept approval does not approve release. No code change may ride on an older approval.

## After approval

- Recheck current task/revision and authority before the effect; if code, requested scope or deployed binding changed, hold and rerun affected gates/approval.
- Publish only the approved task delta and accepted dependencies through the protected release branch and existing CI: `<actual-commit-and-CI-reference>`.
- Verify the actual production deployment and required behavior against the approved result: `<deployed-identity-and-observed-results>`. A push or queued/green build alone does not establish production success.
- Finish the existing [cleanup gate](cleanup.md): `<exact-absence-evidence>`.

Only production verification **and** physical cleanup permit `completed`. Preserve compact source, CI, approval and result references outside disposable roots. Failures stay explicit on this task; do not invent automatic rollback. Archive never rolls back already-deployed code, and an unexpected closed/moved/missing/access state is a non-destructive hold, not cleanup authorization.
