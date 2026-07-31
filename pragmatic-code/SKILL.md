---
name: pragmatic-code
description: Use when implementing or reviewing code pragmatically.
version: 1.0.0
author: rs-radic
license: MIT
platforms: [linux, macos, windows]
tags: [implementation, code-review, simplicity, yagni, security, evidence]
---

# Pragmatic Code

## Overview

Implement the smallest correct solution to the actual requirement. Review code aggressively enough to catch real logic and security defects, but do not turn unsupported possibilities into blockers or implementation work.

Simplicity never means removing a control that protects a real trust boundary, authorization decision, secret, data integrity guarantee, or other demonstrated risk.

## When to Use

Use for:

- implementing or modifying code;
- fixing defects;
- reviewing diffs, commits, and pull requests;
- evaluating proposed fixes or reviewer findings;
- simplifying a solution that has grown beyond its requirement.

Do not use it to narrow an explicitly requested architecture study, compatibility project, threat model, or exhaustive security audit. Even then, keep findings grounded and fixes proportionate.

## Shared Rules

1. **Solve today's requirement.** Do not build for hypothetical future requirements.
2. **Prefer the existing path.** Reuse project code, the standard library, framework behavior, and platform capabilities before adding dependencies or machinery.
3. **Keep the change local.** Avoid drive-by refactors, generalized configuration systems, new frameworks, and unrelated cleanup.
4. **Trace every line.** Every changed line must support the request, a demonstrated defect, or required verification.
5. **Abstract after repetition.** Do not create an abstraction for one use. Extract only when repeated use or a present requirement makes it simpler.
6. **Handle possible failures.** Add error handling for operations that can fail in the supported environment, not for scenarios excluded by a documented contract or proven invariant.
7. **Respect tests.** Never weaken or rewrite tests to justify invented behavior. Change a test only when the requirement or verified behavior contract changed, and state why.
8. **Preserve real protections.** Do not remove reachable authorization, validation, injection protection, secret handling, path controls, deserialization safeguards, data-loss protection, or transaction/concurrency guarantees merely to reduce code.
9. **Avoid fake precision.** Use evidence-backed confidence, not invented numeric percentages or arbitrary complexity thresholds.

## Implementation Mode

### 1. Ground the Requirement

State the requested behavior and inspect the directly affected flow: callers, existing helpers, validation, framework guarantees, tests, deployment constraints, and supported inputs. Do not design before this context is understood.

**Complete when:** the desired behavior and affected execution path can be stated without assumptions.

### 2. Apply the Addition Gate

For every non-obvious branch, abstraction, dependency, configuration option, compatibility path, or error handler, answer:

1. What current requirement or demonstrated defect does it serve?
2. Is it needed now?
3. Is there a simpler solution using the existing design?
4. What evidence proves it is needed?
5. What actually breaks without it?

Classify the addition:

- **`justified`** — directly required now or closes a demonstrated defect.
- **`needs_evidence`** — potentially relevant, but a material premise is unverified. Inspect or ask before adding it.
- **`unjustified`** — speculative, redundant, unrelated, or more complex than the proven need. Do not add it.

**Complete when:** every non-obvious addition is justified and `needs_evidence` items have been resolved rather than silently implemented.

### 3. Implement and Verify

Make the smallest local change that satisfies the grounded requirement. Add or update focused tests where behavior changed, then run the relevant tests and checks.

Do not add another feature because it is adjacent. Do not broaden a fix into a reusable subsystem unless the current requirement already needs that subsystem.

**Complete when:** the requested behavior works, relevant tests pass, and no changed line lacks a present reason.

## Review Mode

### 1. Understand the Real Flow

Review the changed code in context, not as an isolated diff. Inspect only as far as needed to establish:

- actual callers and reachable inputs;
- existing validation and authorization;
- language and framework behavior;
- database constraints and transaction boundaries;
- tests and supported-input contracts;
- deployment and trust boundaries.

A missing fact is a reason to verify or ask, not permission to assume the most alarming case.

### 2. Prove Each Finding

Before reporting a finding, establish:

1. **Claim** — the exact incorrect behavior or violated requirement.
2. **Location** — the relevant file and line.
3. **Mechanism** — how the code produces the result.
4. **Trigger path** — the reachable input, state sequence, actor action, or concurrency interleaving.
5. **Controls considered** — why existing validation, authorization, framework behavior, invariants, deployment boundaries, constraints, or tests do not prevent it.
6. **Impact** — what realistically breaks, leaks, corrupts, or becomes exposed.
7. **Minimal correction** — the smallest change that breaks the demonstrated failure path.

If mechanism, reachability, and material impact cannot be established, it is not a blocking finding.

### 3. Separate Evidence, Impact, and Disposition

Do not collapse these into one severity label.

**Evidence state**

- **Proven** — reproduced or statically conclusive; mechanism, reachability, and impact are established.
- **Needs verification** — strongly supported, but one relevant fact remains unresolved. Verify or ask; do not present it as a confirmed defect.
- **Hypothesis** — mechanism, reachability, or impact is missing. Non-blocking and omitted by default.
- **Refuted** — a verified invariant, validation layer, framework guarantee, deployment boundary, or supported-input contract prevents the claim. Omit it.

**Impact**

Assess realistic consequence independently: critical, high, medium, or low. Certainty does not make a low-impact issue severe, and severe theoretical impact does not make an unsupported claim real.

**Disposition**

- **Blocker** — a proven, current, material defect introduced, exposed, or worsened by the change.
- **Non-blocking** — a grounded but minor issue that does not justify delaying the change.
- **Verify** — a material unanswered question whose answer can change the verdict.
- **Omit** — speculative, refuted, duplicate, purely stylistic, outside scope, or unactionable.

For a high-impact `Needs verification` security concern, stop and resolve the missing fact before approval; do not inflate it into a confirmed vulnerability.

### 4. Security Evidence Gate

Security remains a top priority. A security finding needs a grounded threat path:

1. attacker-controlled input or relevant actor capability;
2. a reachable path to the dangerous operation;
3. a missing or inadequate control;
4. a concrete security consequence.

A live exploit is not mandatory when static data flow and missing control prove the vulnerability. Conversely, a vulnerability label or generic best practice is not evidence by itself.

Give immediate attention to grounded authorization or authentication failures, injection, secret exposure, unsafe deserialization, path traversal, material data loss or corruption, concrete concurrency or transaction failures, and broken trust-boundary validation. Rarity does not excuse a real high-impact path; unsupported prerequisites do not create one.

Run a deliberate false-positive pass before reporting any security finding: argue the strongest case that existing controls or constraints prevent it, then retain the finding only if the evidence survives.

### 5. Prefer the Minimal Correction

Preserve a real finding while rejecting an overengineered fix. Use the project's existing mechanism when adequate. Do not turn one defect into a new framework, broad refactor, generalized policy engine, speculative compatibility layer, or unrelated hardening project.

### 6. Stop Rule

Once the changed flow is understood, relevant tests pass, and no grounded blocker remains:

> Approve the change. Stop searching for something to object to.

Do not invent a replacement suggestion so the review appears productive. `No actionable findings.` is a successful result.

## Output Contract

For implementation work, report:

- the behavior implemented;
- the files changed;
- the verification actually run;
- any unresolved `needs_evidence` item.

For review, list findings first, ordered by impact. Each reported item should contain:

```text
[blocker | non-blocking | verify] file:line — claim
Evidence/path: concrete mechanism and reachable trigger
Impact: realistic consequence
Evidence state: Proven | Needs verification
Minimal fix: smallest adequate correction
```

Omit hypothetical and refuted items unless the user explicitly asks for rejected reasoning. If nothing survives the gate, respond with `No actionable findings.` and stop.

## Common Pitfalls

1. **Security theater:** adding controls without an attacker path or protected asset. Prove the path first.
2. **Premature dismissal:** calling a real vulnerability "unlikely" despite conclusive reachability and high impact. Likelihood and impact are separate.
3. **Diff-only review:** ignoring callers, middleware, framework guarantees, or deployment constraints. Inspect enough context to validate the claim.
4. **Scope expansion:** turning a local change into a repository-wide audit. Pre-existing issues matter only when the change creates, exposes, or worsens them, unless a broader audit was requested.
5. **Fix inflation:** replacing a small defect with an abstraction or subsystem. Break the demonstrated path with the smallest adequate change.
6. **Test rationalization:** modifying tests because the implementation disagrees with them. Resolve the requirement first.
7. **Endless review:** continuing after tests pass and no grounded blocker remains. Approve and stop.

## Verification Checklist

- [ ] The current requirement and affected flow are understood.
- [ ] Every non-obvious addition passed the addition gate.
- [ ] Every reported finding has exact evidence, a reachable mechanism, and realistic impact.
- [ ] Existing controls and deployment constraints were checked.
- [ ] Evidence confidence and impact were assessed independently.
- [ ] Genuine security and data-integrity defects were not suppressed.
- [ ] Speculation did not become a blocker or implementation task.
- [ ] The proposed fix is the smallest adequate correction.
- [ ] Relevant tests/checks were actually run.
- [ ] Review stopped once no grounded blocker remained.
