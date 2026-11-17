# Independent review and shared DEV E2E

Follow the [workflow](../README.md). Remain in `security/spec review` until all required evidence passes.

## Inputs and identity

- Same task/native cards, canonical context/authority and permitted effects: `<verified-references>`.
- Concept approval, actual eligible author, explicit action, completed revision and freshness: `<approval-evidence>`.
- Exact frozen source/artifact identity, fresh DEV baseline, accepted dependencies, worktree ownership: `<bindings>`.
- Implementor, separate SPEC profile, separate quality/security profile, integrator and independent E2E profile: `<role-and-author-provenance>`.
- Required acceptance criteria, unchanged test obligations and UI/browser/visual-QA skills: `<required-reads>`.

## Ordered evidence

1. **SPEC:** use the native same implementation card with the independent `spec-reviewer` explicitly assigned. Verify scope/project invariants against the frozen revision. Verdict, author identity, source binding and findings: `<evidence>`.
2. **Quality/security:** separate `quality-security-reviewer`, after SPEC PASS, without editing the candidate. Verdict, author identity, source binding, security/effect-boundary checks and findings: `<evidence>`.
3. **Shared DEV:** existing integrator reconciles current remote drift, integrates accepted changes/dependencies, and actually deploys shared DEV without overwriting another concept. Record resulting SHA/artifact and runtime binding: `<deployment-evidence>`.
4. **E2E:** independent `e2e-tester` verifies that exact deployed shared DEV revision, not merely an isolated concept preview. Record environment/URL, deployed identity, required browser/functional checks, actual results and failures: `<evidence>`.

A PASS field alone does not prove provenance or independence. Keep approval-author checks separate from technical success. Reconcile any source/artifact differences before accepting evidence as covering the deployed result; do not silently relabel old results current.

Failures return to the same task's correction loop and existing native cards. Later code or requested revisions invalidate affected approval/review/E2E evidence. Only all-pass, correctly bound shared DEV evidence permits `technical approval`; it does not itself authorize release.

Use concise task text, preview links and durable result references, not screenshot uploads. Keep real mail/forms/database/provider/job and business effects blocked absent their existing separate authority.
