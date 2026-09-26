---
name: plan-review-gate
description: Use when a Hermes Kanban plan needs independent review.
version: 1.0.0
author: rs-radic
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [hermes, kanban, planning, review]
    related_skills: [approval-gated-kanban-development, sdlc-review]
---

# Independent Hermes Kanban plan review

Apply only to **new, explicitly opted-in planning cards**; do not retrofit a completed or running card. Hermes dispatches the independent reviewer on the same task's native `review` lane. This procedure reviews a proposal, **not** implementation code; product approval remains with the owner.

## Main chat

- Check the exact board for a matching planner card; avoid duplicate planning or execution graphs. Create a neutral, planning-only scratch card with source documents, scope, exclusions, and `plan-review-gate` force-loaded, assigned to `planner`. Subscribe the originating chat.
- Require the planner to attach an immutable plan or evidenced no-work inventory, read back stored bytes and hash, then call `kanban_request_review(reviewer="plan-reviewer")` on the **same card**, not `kanban_complete`.
- When the reviewer completes with PASS, read the exact reviewed attachment and its active predecessors, verify hashes and status, summarize remaining owner decisions, and request separate owner approval before any execution. An open `review` card or a planner handoff is not a PASS.

## Planner

1. Independently inspect the source record and board, distinguishing approved behavior, implementation choices, in-flight work, unapproved proposals, and exclusions. Choose only eligible scope. If none exists, produce an evidenced no-work map rather than invented work.
2. Include dependencies, API/data/rollback effects where applicable, boundaries and unanswered product decisions, card DAG, branch lanes, acceptance criteria, security, unit/integration/E2E and cleanup evidence. Do not implement or change other cards.
3. Attach the proposal, read back exact bytes, record SHA-256, and request native review by `plan-reviewer` with artifact path/revision/source metadata. On changes requested, write a **new linked revision**, retain prior attachments and explain supersession; request review again. Block only for genuine owner decisions.

## Independent plan reviewer

1. Verify the `review` lane and reviewer assignment. Independently inspect original sources, complete stored proposal and predecessor revisions, their hashes, current board, and applicable project rulings—not just the planner's summary. Do not edit artifacts, code, refs, or other cards.
2. Trace material assertions to source: scope and omissions, ownership boundaries, choices vs requirements, existing or queued work, true dependencies and promotion order. Check risk-bearing capacity, retry/expiry, authorization, data minimization, concurrency, rollback with retained data and exact-candidate E2E only where applicable. Say when a check is not applicable.
3. On a correctable issue record exact evidence and `kanban_request_changes` **on that same task**. A needed business decision is blocked for owner input; do not guess. On PASS call `kanban_complete` with artifact SHA-256, sources, evidence and limits. PASS never authorizes implementation.

## Completion check

The task shows planner `review_requested` and an independent reviewer PASS at the same attachment hash, or a recorded changes-requested/revision/re-review trail ending in PASS. The project repo, live execution graph, `dev`, `main`, and deployment were untouched by the planning gate.
