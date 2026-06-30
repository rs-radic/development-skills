---
name: mockup-driven-ui-implementation
description: Use when implementing or revising a live UI from a screenshot, mockup, design target, or user-provided visual reference. Establishes a user-approved visual contract, preserves real app behavior and data, implements one region at a time, uses mockup-derived or image-edited assets when needed, and requires live-vs-mockup screenshot comparison plus final UI visual QA.
version: 1.0.0
author: rs-radic
license: MIT
platforms: [linux, macos, windows]
tags: [ui, mockups, visual-implementation, screenshots, image-to-image, responsive, frontend]
related_skills: [ui-deliverable-visual-qa, documentation-video-walkthrough]
---

# Mockup-Driven UI Implementation

## Overview

Use this skill when a user wants a live UI to match a screenshot, mockup, generated design, visual comp, or reference image. The mockup is not vague inspiration; it is the visual target. The implementation succeeds only when the rendered UI is compared against that target and the user-approved scope is satisfied.

This skill is for the build discipline before final visual QA. It turns the mockup into a precise visual contract, separates in-scope from out-of-scope regions, preserves real application behavior, implements one region at a time, and verifies the live rendered result against the mockup with screenshot evidence.

Always finish by applying `ui-deliverable-visual-qa` to the exact rendered deliverable.

## When to Use

Use when the request includes any of these:

- "match this mockup"
- "make the live page look like this screenshot"
- "implement this design"
- "use this image as the target"
- "the rendered version still does not match"
- "compare what you rendered to the mockup"
- visual corrections about spacing, art, cards, markers, checkmarks, bullets, colors, image clipping, or mobile drift
- a live UI that must preserve real data/behavior while adopting a new visual layout

Do not use for:

- pure visual QA after implementation is already complete; use `ui-deliverable-visual-qa`
- backend-only changes with no rendered UI target
- freeform redesign where no concrete visual target exists
- marketing/image generation tasks that do not need a live UI implementation

## Core Principle

> The mockup is the visual source of truth for the approved scope, and the live rendered UI must be compared against it before completion.

Do not infer approval to redesign beyond the target. Do not add extra titles, sections, labels, effects, colors, or layout changes because they seem better. If the mockup conflicts with real app behavior, product constraints, accessibility, or live data, discuss the tradeoff with the user before choosing a direction.

## 1. Discuss the Scope Contract First

Before editing code, discuss and confirm the implementation contract with the user whenever the answer is not already obvious.

Confirm:

- Which parts of the mockup are in scope.
- Which parts are out of scope, such as global header, global footer, existing navigation, ads, browser chrome, or unrelated surrounding UI.
- Whether the live app must preserve existing data-driven content, routes, filters, forms, search, ordering, auth, permissions, tracking, pricing, inventory, or state transitions.
- Which mockup details are strict: colors, typography, card shape, image placement, icons, markers, checkmarks, bullets, copy, spacing, and responsive layout.
- Which details may adapt to the real product: longer labels, real rows, real statuses, missing assets, accessibility fixes, or customer-safe copy.
- Which viewports matter: desktop, tablet, mobile, or specific dimensions supplied by the user.

Completion criteria:

- The implementation scope is explicit.
- Out-of-scope regions are named.
- Live behavior/data that must be preserved is named.
- Known conflicts between mockup and real app behavior are surfaced before implementation.

## 2. Inventory Target and Live State

Collect the mockup/reference image and the current live rendering before changing anything when possible.

For the mockup:

- Save or locate the source image path.
- Inspect it visually, not just from the filename.
- Identify sections, components, art assets, markers, spacing patterns, and copy.
- Crop important regions when a full-page mockup is too large to reason about precisely.

For the live UI:

- Capture current screenshots if revising an existing screen.
- Identify the code/templates/styles/scripts responsible for the target region.
- Identify the data sources and behavior that must remain live.
- Confirm the app can be rendered locally or in a test environment.

Completion criteria:

- Mockup/source paths are known.
- Current live baseline exists or a limitation is stated.
- The relevant code/data paths are identified.
- The target can be rendered for screenshot comparison, or a blocker is stated.

## 3. Decompose the Mockup Into Visual Claims

Break the target into regions small enough to implement and verify.

For each region, extract concrete visual claims:

- layout: columns, grid, stacking, order, alignment
- component shape: cards, borders, shadows, radii, dividers
- typography: heading scale, weight, color, line-height
- markers: checkmarks, bullet dots, numbered steps, icons
- art: image source, crop, position, scale, transparency, clipping behavior
- copy: exact text when strict, customer-safe equivalent when approved
- spacing: gaps between cards, sections, controls, and text groups
- responsive behavior: how the region changes at mobile/tablet widths

Use precise claims rather than vague goals:

```text
Network card uses amber bullet dots, not checkmarks.
Gauge art sits in the lower-right of the card and remains fully inside the card on mobile.
The global site header is not part of the mockup scope and remains unchanged.
Server rows remain data-driven; do not replace them with static mockup rows.
```

Completion criteria:

- Every implemented region has a written visual contract.
- User-called-out details are explicitly listed.
- The contract distinguishes strict target details from acceptable adaptations.

## 4. Preserve Real App Behavior and Data

A mockup implementation is not allowed to break the product to match a static picture.

Preserve unless the user explicitly approves changing it:

- data binding and real rows/items
- detail routes and links
- filters, search, sorting, pagination, and compare/select flows
- auth/permission checks
- form submission and validation behavior
- existing analytics/tracking hooks
- stock/status/pricing/business rules
- accessibility semantics that users rely on
- customer-safe product copy and compliance-sensitive wording

If the mockup shows fake data, placeholder labels, simplified states, or impossible combinations, map the visual pattern onto real data instead of hardcoding the fake content.

Completion criteria:

- Live behaviors that could regress are tested or explicitly scoped out.
- Static mockup placeholders are not copied over real dynamic behavior without approval.
- Any intentional behavior/data change is called out separately from visual implementation.

## 5. Implement One Region at a Time

Work in small visual slices. Do not rebuild the entire page and then discover everything drifted.

Recommended sequence:

1. Choose one region or component group.
2. Implement the smallest code/style change needed for that region.
3. Render the live UI.
4. Capture the matching live screenshot/crop.
5. Compare it to the mockup crop.
6. Fix visible drift before moving to the next region.

Prefer narrow, reversible changes:

- page-scoped CSS over global CSS when the target is page-specific
- component-level markup changes over broad template rewrites
- additive classes over fragile selector overrides when possible
- preserving existing IDs/data attributes used by scripts/tests

Completion criteria:

- The implemented region renders correctly before moving on.
- The surrounding UI still fits and behaves.
- No broad/global styling change was made unless it was explicitly in scope.

## 6. Asset and Image Handling

Mockup-driven UI often fails because art assets drift: generated images are close but not close enough, images clip, backgrounds differ, or decorative elements contain text artifacts. Treat assets as part of the visual contract.

Asset options, in preferred order when fidelity matters:

1. Use existing product/design-system assets if they match the target.
2. Crop or derive clean assets from the provided mockup when rights/scope allow and the user wants exact fidelity.
3. Use image-to-image generation or image editing to create a closer asset from the target/reference.
4. Use text-to-image generation only when there is no adequate source asset, then compare strictly against the mockup.
5. Draw simple vector/CSS assets manually when that is more faithful and controllable than generation.

Rules:

- Do not use generated art blindly. Render it in context and compare it to the mockup.
- If an image has baked-in defects such as clipped corners, wrong background, text artifacts, extra labels, wrong perspective, or incorrect colors, replace or regenerate it rather than masking the defect with CSS.
- If CSS clipping is the problem, fix the container, object-fit, dimensions, padding, overflow, or border-radius instead of regenerating the image.
- Decorative UI art should not contain accidental text, logos, buttons, watermarks, or interface labels unless the mockup requires them.
- Store dimensions and placement intentionally; do not rely on browser auto-sizing when the mockup requires precise alignment.

Completion criteria:

- Every visual asset used by an in-scope region is accounted for.
- Generated or edited assets are compared against the mockup in context.
- Image clipping is classified as either source-image defect or CSS/container defect.
- Defective assets are replaced/regenerated rather than hidden.

## 7. Live-vs-Mockup Comparison Is Mandatory

Do not claim the implementation matches until the live rendered UI has been compared to the mockup.

Minimum evidence:

- screenshot of the live rendered region
- corresponding mockup crop or full target image
- side-by-side comparison, overlay, or clearly paired screenshots
- viewport dimensions used for each comparison
- notes for exact user-called-out details

Compare:

- section order and boundaries
- card positions and sizes
- headings, labels, and copy
- marker style: checkmarks vs bullets vs numbers
- image/art placement, scale, crop, and clipping
- colors and contrast
- spacing and alignment
- live data rows or dynamic content placement
- desktop and mobile behavior

If the user says the comparison was missing, stop implementing and produce the comparison before doing more code changes.

Completion criteria:

- Live render and mockup are visibly compared.
- Every user-called-out issue has a pass/fail statement.
- Remaining drift is either fixed, explicitly accepted by the user, or reported as a blocker.

## 8. Responsive Mockup Translation

A desktop mockup rarely defines mobile perfectly. Do not invent a mobile layout silently when fidelity matters.

When mobile is not specified:

- preserve the desktop visual hierarchy
- stack regions in a predictable order
- keep cards and controls fully inside the viewport
- maintain readable text and tappable controls
- keep art inside its card or region
- avoid horizontal page scroll
- ask the user when multiple mobile translations would be meaningfully different

Minimum practical checks:

- desktop target width or user-provided width
- 390px mobile width
- any additional width where the layout changes or previously failed

Completion criteria:

- The live UI is checked at relevant desktop and mobile widths.
- Mobile adaptations preserve the target intent.
- No card, image, filter, button, label, or table content clips unexpectedly.

## 9. Failure Handling

If the live render does not match the target:

1. Name the visible drift precisely.
2. Determine whether it is caused by markup, CSS, data shape, asset source, viewport, or browser behavior.
3. Fix the root cause narrowly.
4. Re-render and compare again.
5. Do not call the region complete until the screenshot evidence passes or the user accepts the deviation.

Common failure patterns:

- generated art looks polished but not like the mockup
- checkmarks used where the mockup used bullet dots, or vice versa
- image appears inside the card but its source bitmap has clipped corners
- text matches semantically but line breaks/weight/size change the visual hierarchy
- real data is forced into static mockup dimensions and overflows
- mobile viewport meta/config is missing, so mobile CSS never represents a real phone viewport
- full-page screenshot is actually viewport-only
- comparison uses stale screenshots after a fix
- local browser cache hides whether the latest CSS/image asset is loaded

Completion criteria:

- The failed claim is re-tested with fresh screenshots.
- Stale assets/screenshots are not used as proof.
- Final status is pass, accepted deviation, or blocked; never "should be fixed" without a render check.

## 10. Final Visual QA Gate

After the mockup implementation loop passes, load and follow `ui-deliverable-visual-qa` for final delivery.

The final response should include:

```md
Mockup implementation:
- Target/mockup: <path or description>
- Scope: <in-scope regions; out-of-scope regions>
- Preserved behavior/data: <key items>
- Viewports compared: <sizes>
- Live-vs-mockup evidence: <paths>
- User-called-out details:
  - PASS/FAIL: <detail>
  - PASS/FAIL: <detail>
- Remaining drift/accepted deviations: <none or list>

Visual QA:
- Final screenshots: <paths>
- Result: visually acceptable / not acceptable / blocked
```

Do not bury visual drift in a generic success message. If anything remains off, say so.

## Common Pitfalls

1. **Treating the mockup as inspiration.** If the task is to match a mockup, extra creative changes are drift unless approved.
2. **Skipping the scope conversation.** Header/footer/global navigation often should remain live even if the mockup shows different framing.
3. **Hardcoding fake mockup data.** Preserve live data and behavior unless the user asks for a static prototype.
4. **Implementing the whole page before comparing.** Large-batch visual work creates too many simultaneous drift sources.
5. **Using generated assets without comparison.** Generated art must be verified in context against the mockup.
6. **Patching around a bad source image.** If the image itself is wrong or clipped, replace/regenerate it.
7. **Comparing stale screenshots.** Always compare the latest live render after the latest code/style/asset change.
8. **Only checking desktop.** Mockup-driven pages still need mobile overflow and responsive checks.
9. **Claiming match without evidence.** Provide screenshots, side-by-sides, metrics, or explicit visual observations.
10. **Confusing visual match with final QA.** Matching the mockup is necessary but not sufficient; final rendered UI still needs full visual QA.

## Verification Checklist

Before finalizing a mockup-driven UI task, confirm:

- [ ] User-approved scope is clear.
- [ ] Out-of-scope mockup regions are named.
- [ ] Live behavior/data that must be preserved is named.
- [ ] Mockup/source image was inspected visually.
- [ ] Current live baseline was captured or limitation stated.
- [ ] Target was decomposed into region-level visual claims.
- [ ] User-called-out details are explicitly tracked.
- [ ] Implementation was performed region by region.
- [ ] Dynamic data/routes/forms/scripts were preserved or intentional changes were approved.
- [ ] Assets were matched, cropped, edited, generated, or redrawn intentionally.
- [ ] Generated/edited assets were compared against the mockup in context.
- [ ] Any image clipping was classified as source-image vs CSS/container issue.
- [ ] Live-vs-mockup comparison screenshots were produced.
- [ ] Desktop and mobile viewports were checked.
- [ ] No unintended horizontal overflow remains.
- [ ] Stale screenshots/assets were not used as final proof.
- [ ] Remaining drift is fixed, accepted, or reported as blocked.
- [ ] `ui-deliverable-visual-qa` was used as the final QA gate.
