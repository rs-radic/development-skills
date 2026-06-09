---
name: just-keep-swimming
description: Keep a project moving autonomously and durably when the user says "Just Keep Swimming" or asks for continuous progress toward a goal. Use for owner-loop project execution across turns, compactions, restarts, and idle periods with durable goal/state files, recurring cron driver ticks, bounded subagents, verification, blockers, and periodic progress reports.
version: 1.0.0
author: rs-radic
license: MIT
platforms: [linux, macos, windows]
tags: [autonomy, durable-execution, cron, subagents, progress-reporting]
---

# Just Keep Swimming

Use this skill to keep a project moving across chat turns, compactions, restarts, and idle periods without requiring repeated nudges.

Pattern: **one durable goal file + one state file + one recurring driver cron + one reporting cron + bounded work slices + owner verification**.

## Core rules

- Keep one durable owner loop for the project; do not let detached tasks become the source of truth.
- Treat the driver cron as a dead-man/resume trigger, not a long-running worker.
- On each driver tick, do exactly one bounded advancement: verify, start one slice, update state, or record a blocker.
- Do not wait or poll inside cron turns. Spawn bounded work and exit, or run small verification directly.
- Treat subagent output as untrusted until the owner inspects artifacts and runs the smallest meaningful verification.
- Report verified progress only. Do not count "spawned" as "done".
- Preserve user safety constraints and project guardrails in every child prompt.
- On Linux, use bounded `bash`, `rg`, `find`, `timeout`, and explicit paths. Never run broad recursive searches from workspace/project roots.

## Initial setup

Create these files in the project root unless the user requests another location:

- `GOAL.md` — durable human-readable goal, guardrails, queue, and owner rules.
- `goal-state.json` — machine-readable state for driver/reporter.
- Optional project notes such as `STATE.md` if the project already uses them.

If critical inputs are missing, ask one concise question. Otherwise infer sensible defaults and proceed.

Minimum inputs:

- Project root/path
- Final goal / definition of done
- Non-negotiable guardrails
- Progress report destination and cadence
- Whether external/destructive actions are allowed

## `GOAL.md` template

```markdown
# <Project> Goal Owner

## Final goal

<Concrete definition of done. Include acceptance criteria.>

## Hard guardrails

- <Safety/privacy/external-action limits>
- <Test/build/runtime constraints>
- <Data handling rules>
- Never run unbounded recursive searches from the project/workspace root.
- On Linux, use bounded searches with explicit roots, excludes, output caps, and timeouts.

## Owner model

The recurring goal driver owns progress and state. It should:

1. Read `goal-state.json`, `GOAL.md`, and relevant project state/docs.
2. Reconcile completed child work before starting new work.
3. Start exactly one bounded next slice when no slice is active.
4. Prefer subagents for implementation/research, but do not wait inside the driver turn for long work.
5. Run small verification directly when it is three focused commands or fewer.
6. Update `goal-state.json` every tick with active slice, evidence, blockers, next action, and rough percent.
7. Mark true blockers clearly; distinguish them from normal execution gaps.
8. Leave routine user-facing reporting to the reporter cron.

## Queue

1. <First bounded slice>
2. <Second bounded slice>
3. <Final acceptance/evidence bundle>
```

## `goal-state.json` template

```json
{
  "version": 1,
  "goalId": "<short-project-goal-id>",
  "ownerSessionTarget": "session:<short-project-goal-id>",
  "status": "active",
  "roughPercent": 0,
  "createdAt": "<iso timestamp>",
  "updatedAt": "<iso timestamp>",
  "lastHourlyUpdateAt": null,
  "activeSlice": null,
  "lastCompletedSlice": null,
  "lastVerifiedEvidence": [],
  "queue": [
    {
      "id": "first-slice",
      "title": "First bounded slice",
      "status": "pending",
      "notes": ""
    }
  ],
  "blockers": [],
  "guardrails": []
}
```

Slice statuses: `pending`, `next`, `active`, `verification-needed`, `completed`, `blocked`.

## Driver cron

Create one recurring goal-driver cron, usually every 15 minutes.

Preferred properties:

- `sessionTarget`: `session:<goal-id>`
- `payload.kind`: `agentTurn`
- `delivery.mode`: `none`
- `timeoutSeconds`: bounded, usually 600 seconds
- Failure alert: same channel/user as reports

Driver prompt template:

```text
<Project> GOAL DRIVER tick.

You are the durable owner loop for finishing this project. Cron is only a dead-man/resume trigger, not the project brain.

Files:
- `<project>/GOAL.md`
- `<project>/goal-state.json`
- `<project>/STATE.md` if present

Rules:
1. Every tick must either advance state, run one bounded verification/implementation action, spawn exactly one bounded child, or write a concrete blocker.
2. A tick that only observes an active/verification-needed state and exits is a failure.
3. If `activeSlice.status` is `verification-needed`, run the narrow verification now or mark an explicit blocker.
4. If `activeSlice.status` is `active` for too long with no observable progress, treat it as stale: inspect available artifacts, verify, steer, or mark stale in state.
5. Do not use child subagents for verification-only work when verification is three focused commands or fewer.
6. If no active slice exists and no blocker exists, start the next queued slice.
7. For longer implementation/research, spawn one bounded child and immediately record child session/run/status in `goal-state.json`.
8. Do not wait/poll loops. One action, state update, exit.
9. Do not run heavy full suites/browser/DB/external work unless the current slice explicitly authorizes it and includes safety gates.
10. Preserve all guardrails from `GOAL.md` and `goal-state.json`.
11. On Linux, use `timeout <seconds> <command>`, `rg` with explicit roots/excludes, and capped output.

Exit only after updating `goal-state.json`.
```

## Reporter cron

Create one progress reporter cron, usually hourly.

Preferred properties:

- `sessionTarget`: same `session:<goal-id>`
- `payload.kind`: `agentTurn`
- `delivery`: explicit channel/user target
- `timeoutSeconds`: bounded, usually 600 seconds

Reporter prompt template:

```text
<Project> HOURLY PROGRESS UPDATE.

Read `<project>/GOAL.md`, `<project>/goal-state.json`, and `<project>/STATE.md` if present. Do not run heavy tests/builds/browser/DB/external work in this reporting turn.

Send a concise user-facing update to the configured destination. Include:
- rough full-project percent
- current active slice or next slice
- what was verified/completed since the last update
- blockers, if any
- what will happen next

Be honest if no verified progress happened. Do not expose secrets or internal session metadata. After reporting, update `goal-state.json.lastHourlyUpdateAt` and `updatedAt`.
```

## Child slice prompts

Keep child prompts narrow and artifact-driven:

```text
Work on exactly this slice: <slice title>.

Project root: <path>
Goal files: `GOAL.md`, `goal-state.json`, relevant docs.

Deliverables:
- Write/update specific artifacts: <paths>
- Run at most these checks: <commands or limits>
- Return concise summary of changed files and verification.

Forbidden:
- Anything outside this slice
- External/destructive actions not explicitly authorized
- Secrets in chat/docs
- Broad recursive searches or large dump output
- Long full-suite/browser/DB/provider work unless explicitly authorized
```

## Verification and acceptance

Use the smallest meaningful gate:

- focused tests, lint, typecheck, build, screenshot, direct inspection, or source diff
- Linux safety search with explicit roots/excludes, for example: `timeout 20s rg -n --glob '!node_modules' --glob '!dist' '<pattern>' <explicit-root> | head -80`
- state/doc inspection for claimed artifacts

Only after verification:

1. Mark the slice `completed`.
2. Add concise evidence to `lastVerifiedEvidence`.
3. Update rough percent.
4. Set the next slice to `next` or `active`.
5. Update project docs/state.

If verification fails, record the exact failure and either fix it directly or start one bounded fix slice.

## Blocker handling

Use `blocked` only when progress genuinely requires unavailable credentials, access approval, a human decision, or an external dashboard/account capability.

If the agent can research, implement, test, or derive a fixture safely, it is an execution gap, not a blocker.

## Delivery rules

- For group/channel work, use explicit delivery targets for reports and failure alerts.
- Do not DM progress reports unless requested.
- Keep routine driver ticks silent; only reporter posts scheduled progress.

## Linux search and output safety

- Never run unbounded recursive searches from workspace/project roots.
- Avoid broad `find /`, broad `grep -R`, or uncapped command output.
- Prefer `rg` with explicit roots, excludes, `--max-count`, `head`, and `timeout`.
- Exclude backups/imports, `.secrets`, build outputs, test results, dumps, archives, `node_modules`, `.git`, `dist`, and `coverage` unless explicitly needed.
- Never stream large dump/backup contents into chat/tool output; write bulky evidence to files and return small excerpts.

## Restart/compaction survival

Before ending major work or after accepting a slice:

- Update `goal-state.json`.
- Update `GOAL.md`/`STATE.md` or project docs when guardrails or decisions change.
- Record durable decisions in project memory/notes if available.
- Make the next action obvious from files alone.

## Completion

When final acceptance criteria are met:

1. Run the agreed final verification/evidence pass.
2. Mark `goal-state.json.status = "completed"`.
3. Disable or remove the driver cron.
4. Disable or remove the reporter cron after final report.
5. Send a concise final summary with evidence and caveats.
