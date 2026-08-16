---
name: pragmatic-code
description: Use when implementing or reviewing code pragmatically.
version: 1.1.0
author: rs-radic
license: MIT
platforms: [linux, macos, windows]
tags: [implementation, code-review, simplicity, yagni, security, evidence]
---

# Pragmatic Code

## Overview

Implement the smallest correct solution to the actual approved requirement. Review code aggressively enough to catch real logic and security defects, but do not turn unsupported possibilities into blockers or implementation work.

A defect being real, a fix being reasonable, and the agent being authorized to implement that fix are three separate decisions. Review findings do not expand scope. Simplicity never means removing a control that protects a real trust boundary, authorization decision, secret, data integrity guarantee, or other demonstrated risk.

## When to Use

Use for:

- implementing or modifying code;
- fixing defects;
- reviewing diffs, commits, and pull requests;
- evaluating proposed fixes or reviewer findings;
- simplifying a solution that has grown beyond its requirement.

Do not use it to narrow an explicitly requested architecture study, compatibility project, threat model, or exhaustive security audit. Even then, keep findings grounded, fixes proportionate, and implementation within the user's approved scope.

## Shared Rules

1. **Solve today's approved requirement.** Do not build for hypothetical future requirements.
2. **Prefer the existing path.** Reuse project code, the standard library, framework behavior, and platform capabilities before adding dependencies or machinery.
3. **Keep the change local.** Avoid drive-by refactors, generalized configuration systems, new frameworks, and unrelated cleanup.
4. **Trace every line to authority.** Every changed line must map to an explicit approved requirement, a regression introduced by the current work, or required verification that does not alter product behavior.
5. **File scope is not behavior scope.** A file named in a plan authorizes only the described behavior in that file; it is not permission to fix or redesign everything encountered there.
6. **Findings are not authorization.** A proven defect, security concern, reviewer blocker, or minimal fix still needs approval when it is pre-existing, adjacent, or otherwise outside the approved behavior.
7. **Abstract after repetition.** Do not create an abstraction for one use. Extract only when repeated use or a present approved requirement makes it simpler.
8. **Handle required failures.** Add error handling for operations that can fail in the supported environment only when the approved behavior requires it or the current change would otherwise introduce a regression.
9. **Respect tests and scope.** Never weaken tests to justify invented behavior, and never add a failing test to turn unapproved behavior into an apparent requirement.
10. **Preserve real protections.** Do not remove reachable authorization, validation, injection protection, secret handling, path controls, deserialization safeguards, data-loss protection, or transaction/concurrency guarantees merely to reduce code. If preserving or changing one exceeds scope, stop and ask.
11. **Avoid fake precision.** Use evidence-backed confidence, not invented numeric percentages or arbitrary complexity thresholds.

## Scope Authority

### Approval Sources

Treat behavior as approved only when it is grounded in an authoritative source:

- the user's current request or later explicit decision;
- a user-approved written plan, specification, or acceptance criterion;
- a project rule the user has adopted for the current work.

Record the exact source: requirement or task identifier, plan section, or user message. A later specific instruction overrides an earlier broad process clause.

The following are not approval sources:

- an implementation agent's own classification;
- a reviewer prompt that merely labels behavior "approved";
- a file allowlist;
- a failing test written during the current work;
- an adjacent code path or shared caller;
- phrases such as "same path," "same rule," "scope-adjacent," "needed for correctness," "defensive," "hardening," or "smallest fix";
- a generic plan instruction to fix review findings when the user separately requires approval for unplanned changes.

### Scope Classes

Classify every semantic product change before editing:

- **`approved_requirement`** — directly implements behavior in an authoritative approval source.
- **`introduced_regression`** — corrects behavior introduced by the current work that violates an approved requirement or fails to preserve the established baseline where preservation was required.
- **`needs_approval`** — a proven pre-existing or adjacent defect, security improvement, hardening, cleanup, consistency change, extra caller, new behavior, or ambiguous interpretation not explicitly approved.
- **`out_of_scope`** — unrelated, rejected, speculative, or prohibited work. Do not implement it.

Only `approved_requirement` and `introduced_regression` may proceed without new approval. If classification is uncertain, use `needs_approval` and ask before changing code, tests, schemas, configuration, or generated artifacts.

A necessary implementation detail may remain under `approved_requirement` only when it has no separately observable product behavior, uses the existing design, and is the smallest way to satisfy the approved outcome. If it changes failures, side effects, messages, redirects, persistence, authorization, retries, timing, or external calls, treat it as semantic behavior and require explicit provenance.

## Implementation Mode

### 1. Ground the Requirement and Baseline

State the requested behavior and inspect the directly affected flow: callers, existing helpers, validation, framework guarantees, tests, deployment constraints, supported inputs, and relevant parent/baseline behavior. Do not design before this context is understood.

Create a compact scope ledger:

```text
Behavior | Scope class | Approval provenance | Baseline behavior | Intended behavior
```

Do not substitute a changed-file list for this semantic ledger.

**Complete when:** the desired behavior, affected execution path, baseline, explicit exclusions, and approval provenance can be stated without assumptions.

### 2. Apply the Scope Authority Gate

For each proposed behavior change, ask:

1. What exact approved requirement authorizes it?
2. Where is that approval recorded?
3. Is this behavior already present in the baseline?
4. Was the defect introduced by the current work or merely discovered nearby?
5. Does the change alter observable behavior beyond the approved outcome?

If a reviewer discovers a real pre-existing defect, report it with impact and the minimal fix, mark it `needs_approval`, and stop that change. Do not implement it, add a regression test for the proposed new behavior, or rewrite the review brief to call it approved.

If the current implementation introduced the defect, correct it only to restore the approved behavior or required baseline. Do not use the correction as an opening for broader cleanup.

**Complete when:** every planned semantic change is `approved_requirement` or `introduced_regression`; all `needs_approval` items have an explicit user decision.

### 3. Apply the Addition Gate

For every non-obvious branch, abstraction, dependency, configuration option, compatibility path, error handler, schema element, or background mechanism, answer:

1. Which authorized behavior does it serve?
2. Is it needed now?
3. Is there a simpler solution using the existing design?
4. What evidence proves it is needed?
5. What actually breaks without it?

Classify the addition:

- **`authorized`** — required by an `approved_requirement` or the minimal repair for an `introduced_regression`.
- **`needs_evidence`** — potentially authorized, but a material premise is unverified. Inspect or ask, then reclassify before editing.
- **`needs_approval`** — useful or defect-driven but not authorized. Present it separately and wait.
- **`unjustified`** — speculative, redundant, unrelated, or more complex than the proven need. Do not add it.

A demonstrated defect is evidence that a problem exists; it does not by itself move an addition to `authorized`.

**Complete when:** every non-obvious addition is `authorized`, and all other classes have been resolved without silent implementation.

### 4. Implement and Verify

Make the smallest local change that satisfies the authorized behavior. Add or update focused tests only for approved behavior changes or current-work regressions, then run the relevant tests and checks.

Do not add another feature because it is adjacent. Do not broaden a fix into a reusable subsystem unless the current requirement already needs that subsystem. Do not modify a pre-existing defect merely because the affected file is already open or included in the plan.

Before staging or delivery, perform a semantic scope audit:

```text
Behavior changed | Approval provenance | Test/evidence | Residual risk
```

Any behavior without provenance must be removed from the candidate or presented for approval.

**Complete when:** the approved behavior works, relevant tests pass, every changed behavior has authority, and no changed line lacks a present authorized reason.

## Review Mode

### 1. Understand the Real Flow

Review the changed code in context, not as an isolated diff. Inspect only as far as needed to establish:

- actual callers and reachable inputs;
- existing validation and authorization;
- language and framework behavior;
- database constraints and transaction boundaries;
- tests and supported-input contracts;
- deployment and trust boundaries;
- parent/baseline behavior;
- the authoritative approved scope.

A missing fact is a reason to verify or ask, not permission to assume the most alarming case or broaden scope.

### 2. Prove Each Finding

Before reporting a finding, establish:

1. **Claim** — the exact incorrect behavior or violated requirement.
2. **Location** — the relevant file and line.
3. **Mechanism** — how the code produces the result.
4. **Trigger path** — the reachable input, state sequence, actor action, or concurrency interleaving.
5. **Controls considered** — why existing validation, authorization, framework behavior, invariants, deployment boundaries, constraints, or tests do not prevent it.
6. **Impact** — what realistically breaks, leaks, corrupts, or becomes exposed.
7. **Baseline** — whether the same behavior exists in the parent revision.
8. **Minimal correction** — the smallest change that breaks the demonstrated failure path.

If mechanism, reachability, and material impact cannot be established, it is not a blocking finding.

### 3. Establish Scope and Approval Provenance

For every surviving finding, independently classify:

- **Scope status:** `approved_requirement`, `introduced_regression`, `needs_approval`, or `out_of_scope`.
- **Approval provenance:** exact requirement, plan section, or user decision; use `none` when absent.
- **Introduced by current diff:** `yes`, `no`, or `uncertain`.

Do not trust a parent agent's use of the word "approved" without checking the cited source. A reviewer may reject a candidate because of a proven defect, but that verdict does not authorize the implementation agent to fix an unapproved pre-existing or adjacent behavior.

"Exposed by the change" means the diff makes the defect newly reachable or materially changes its impact. Merely touching the file, routing through a new wrapper with equivalent semantics, or noticing an existing defect during review does not make it introduced or exposed.

### 4. Separate Evidence, Impact, Disposition, and Authority

Do not collapse these into one severity label.

**Evidence state**

- **Proven** — reproduced or statically conclusive; mechanism, reachability, and impact are established.
- **Needs verification** — strongly supported, but one relevant fact remains unresolved. Verify or ask; do not present it as a confirmed defect.
- **Hypothesis** — mechanism, reachability, or impact is missing. Non-blocking and omitted by default.
- **Refuted** — a verified invariant, validation layer, framework guarantee, deployment boundary, or supported-input contract prevents the claim. Omit it.

**Impact**

Assess realistic consequence independently: critical, high, medium, or low. Certainty does not make a low-impact issue severe, and severe theoretical impact does not make an unsupported claim real.

**Disposition**

- **Blocker** — a proven, current, material defect introduced or materially worsened by the change, or a direct violation of an approved requirement.
- **Non-blocking** — a grounded but minor issue that does not justify delaying the change.
- **Verify** — a material unanswered question whose answer can change the verdict.
- **Omit** — speculative, refuted, duplicate, purely stylistic, outside review scope, or unactionable.

**Implementation authority**

- **Authorized** — `approved_requirement` or `introduced_regression` with exact provenance.
- **Needs approval** — real finding, but no authority to change behavior.
- **Prohibited** — explicitly excluded or rejected.

A `Needs approval` finding may be important enough to pause release and ask the user, but it must not be silently fixed. For a high-impact `Needs verification` security concern, resolve the missing fact before approval; do not inflate it into a confirmed vulnerability.

### 5. Security Evidence Gate

Security remains a top priority. A security finding needs a grounded threat path:

1. attacker-controlled input or relevant actor capability;
2. a reachable path to the dangerous operation;
3. a missing or inadequate control;
4. a concrete security consequence.

A live exploit is not mandatory when static data flow and missing control prove the vulnerability. Conversely, a vulnerability label or generic best practice is not evidence by itself.

Give immediate attention to grounded authorization or authentication failures, injection, secret exposure, unsafe deserialization, path traversal, material data loss or corruption, concrete concurrency or transaction failures, and broken trust-boundary validation. Rarity does not excuse a real high-impact path; unsupported prerequisites do not create one.

Run a deliberate false-positive pass before reporting any security finding: argue the strongest case that existing controls or constraints prevent it, then retain the finding only if the evidence survives. If a real security defect is pre-existing and outside approved scope, report and escalate it; do not hide it or self-authorize its fix.

### 6. Prefer the Minimal Authorized Correction

Preserve a real finding while rejecting an overengineered fix. Use the project's existing mechanism when adequate. Do not turn one defect into a new framework, broad refactor, generalized policy engine, speculative compatibility layer, or unrelated hardening project.

Minimality does not create authority. Propose the smallest correction, but implement it only when its scope status is authorized.

### 7. Stop Rule

Once the changed flow is understood, relevant tests pass, no grounded blocker remains, and every implemented behavior has approval provenance:

> Approve the change. Stop searching for something to object to.

Do not invent a replacement suggestion so the review appears productive. `No actionable findings.` is a successful result.

## Output Contract

For implementation work, report:

- the approved behavior implemented and its provenance;
- the files changed;
- the verification actually run;
- any unresolved `needs_evidence` item;
- any discovered `needs_approval` item that was not implemented.

For review, list findings first, ordered by impact. Each reported item should contain:

```text
[blocker | non-blocking | verify] file:line — claim
Evidence/path: concrete mechanism and reachable trigger
Impact: realistic consequence
Evidence state: Proven | Needs verification
Baseline: introduced by this diff | pre-existing | uncertain
Scope status: approved_requirement | introduced_regression | needs_approval | out_of_scope
Approval provenance: exact source | none
Minimal fix: smallest adequate correction
Implementation authority: Authorized | Needs approval | Prohibited
```

Omit hypothetical and refuted items unless the user explicitly asks for rejected reasoning. If nothing survives the gate, respond with `No actionable findings.` and stop.

## Common Pitfalls

1. **Scope laundering:** calling unplanned behavior "same path," "review-required," or "scope-adjacent" and implementing it without approval. A label does not create authority.
2. **File-scope fallacy:** treating a listed caller or file as permission to change unrelated behavior inside it. Map behavior, not filenames.
3. **Reviewer laundering:** feeding a reviewer an agent-authored list of "approved requirements" without exact user/plan provenance. Review the authoritative source.
4. **Test laundering:** writing a RED test for unapproved behavior and then treating the failure as permission to modify product code. Tests encode scope; they do not create it.
5. **Security theater:** adding controls without an attacker path or protected asset. Prove the path first, then check authority.
6. **Premature dismissal:** calling a real vulnerability "unlikely" despite conclusive reachability and high impact. Likelihood, impact, and implementation authority are separate.
7. **Diff-only review:** ignoring callers, baseline behavior, middleware, framework guarantees, or deployment constraints. Inspect enough context to validate the claim.
8. **Scope expansion:** turning a local change into a repository-wide audit or automatically fixing pre-existing issues. Report adjacent defects separately unless broader work was approved.
9. **Fix inflation:** replacing a small defect with an abstraction or subsystem. Break the authorized demonstrated path with the smallest adequate change.
10. **Test rationalization:** modifying tests because the implementation disagrees with them. Resolve the requirement and approval source first.
11. **Endless review:** continuing after tests pass, scope is clean, and no grounded blocker remains. Approve and stop.

## Verification Checklist

- [ ] The current requirement, baseline, exclusions, and affected flow are understood.
- [ ] Every semantic behavior change has a scope class and exact approval provenance.
- [ ] A changed-file allowlist was not mistaken for behavior authorization.
- [ ] Pre-existing and adjacent defects were separated from current-work regressions.
- [ ] Every non-obvious addition passed both the scope authority gate and addition gate.
- [ ] No reviewer statement or newly written test silently expanded scope.
- [ ] Every reported finding has exact evidence, a reachable mechanism, realistic impact, and baseline comparison.
- [ ] Existing controls and deployment constraints were checked.
- [ ] Evidence confidence, impact, disposition, and implementation authority were assessed independently.
- [ ] Genuine security and data-integrity defects were reported without self-authorizing out-of-scope fixes.
- [ ] The proposed fix is the smallest adequate authorized correction.
- [ ] Relevant tests/checks were actually run.
- [ ] The final semantic scope audit accounts for every behavior change.
- [ ] Review stopped once no grounded blocker or unapproved change remained.
