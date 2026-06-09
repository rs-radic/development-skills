---
name: ui-deliverable-visual-qa
description: Use when building, editing, or reviewing any UI deliverable. Requires baseline screenshots for existing UI before edits, explicit visible outcome claims, rendered browser/screenshot comparison, responsive viewport checks, and rejection of overflow, clipping, misaligned controls, broken layouts, or disrupted UX flow.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ui, frontend, visual-qa, deliverable, screenshots, responsive, overflow, layout]
    related_skills: [dogfood, requesting-code-review, systematic-debugging, test-driven-development]
---

# UI Deliverable Visual QA

## Overview

Use this skill whenever a task creates, edits, fixes, or reviews a rendered user interface deliverable. It prevents the common agent failure mode of changing code, seeing that the page loads, and declaring success even though the rendered page is visibly broken, incomplete, or not visually verified.

The browser screenshot is the source of truth. Static code review, passing builds, and DOM inspection are not enough for UI work. For existing UI revisions, capture baseline screenshots before changing the UI, then compare final screenshots against that baseline after the change.

This skill borrows the strongest patterns from responsive design checklists, browser verification workflows, visual QA protocols, visible-claim testing, accessibility contrast standards, and dialog/action placement guidance:

- define expected visible outcomes before finalizing
- preserve existing layout relationships unless the user explicitly requested otherwise
- catch overflow, clipping, poor wrapping, pushed navigation, cramped controls, and bad responsive behavior
- catch unreadable low-contrast text, disabled-looking required copy, and invisible control states
- catch illogical button/control placement, scattered modal actions, and broken decision flow
- test realistic desktop and mobile viewport dimensions
- provide screenshot-backed evidence in the final response

## When to Use

Trigger this skill for any user request that touches visible UI, including:

- adding or editing pages, forms, cards, sidebars, menus, tables, modals, tabs, accordions, filters, pagination, dashboards, headers, footers, or mobile navigation
- CSS, layout, spacing, typography, grid, flexbox, overflow, z-index, sticky/fixed positioning, or responsive breakpoint changes
- moving existing controls or adding new controls to an existing screen
- making a UI match a screenshot, mockup, design, customer report, or expected visual outcome
- fixing an issue where something looks wrong, does not fit, wraps badly, overlaps, or pushes another component out of place
- reviewing a PR or patch that affects rendered UI

Do not use this skill for pure backend/API changes with no user-visible rendering unless the backend change changes displayed content, states, error messages, or page flow.

## Core Principle

> A UI change is not done until the rendered UI is visually verified at the relevant viewport sizes.

For existing UI revisions:

> Baseline screenshots are required before work starts. Final screenshots must be compared against the baseline before the task is called complete.

If screenshots cannot be captured because the page is inaccessible, auth is unavailable, the app cannot run, or the browser tool is unavailable, state that as a blocker or limitation. Do not silently downgrade to code-only review.

## Required Protocol for Existing UI Revisions

Existing UI means the task modifies a screen that already exists, even if the change is small.

### 1. Capture baseline before editing

Before making code/CSS/template changes, capture or obtain baseline screenshots of the current UI.

Minimum baseline set:

- the specific screen or component being changed
- enough surrounding page context to see whether menus, sidebars, cards, headers, tables, and main content stay in their intended positions
- every viewport that could plausibly be affected by the edit

If the user attached an original screenshot, treat it as a baseline artifact, but still capture a live baseline when possible. If the code is already modified before this skill loads, reconstruct the baseline from git, a previous deployment, a saved screenshot, or user-provided screenshot. If that is not possible, mark the comparison as limited.

### 2. Identify the visual contract

Before editing, write down the expected change and what must remain stable.

For each affected screen, answer:

- What is supposed to change?
- What must not change?
- Which parent containers, columns, sidebars, menus, cards, tables, and controls define the layout relationship?
- Which states matter: empty, populated, loading, error, disabled, long text, narrow viewport, expanded/collapsed?
- Which viewport dimensions are required by the user or implied by the product?

### 3. Implement narrowly

Make the smallest change that satisfies the requested UI outcome. Do not restructure surrounding layout, navigation, or page hierarchy unless that is the requested change.

When adding controls to existing UI, place them inside the intended parent container and verify they do not consume space that belonged to a sibling component, sidebar, menu, table, or main content area.

### 4. Capture final screenshots

After implementation, capture final screenshots at the same viewport dimensions and UI states as the baseline.

### 5. Compare baseline vs final

Compare before/after screenshots explicitly:

- Expected differences should be visible and limited to the requested change.
- Stable areas should remain visually stable.
- Existing navigation and UX flow should still be recognizable and usable.
- No surrounding component should be pushed, squeezed, overlapped, clipped, hidden, or reflowed into the wrong column.

If the final UI has a visible regression, do not call the task complete. Fix and repeat final screenshot verification.

## Required Protocol for New UI

New UI does not have a previous baseline, so create a visual acceptance contract before finalizing.

Write concrete visible claims such as:

- "At 1366px width, the left sidebar remains left of the main content and does not overlap it."
- "All action buttons fit inside their parent card."
- "Labels and controls remain readable and do not collide."
- "The table remains inside the content panel without horizontal page scroll."
- "At 390px width, controls stack vertically and remain tappable."
- "The primary action remains visible without requiring horizontal scrolling."

Avoid vague claims:

- "Looks good"
- "Layout works"
- "Page is cleaner"
- "The interaction feels smooth"

Every responsive claim must include a viewport width or device class.

## Screenshot Analysis Checklist

When inspecting screenshots, check the full page composition, not only the component that changed.

### Layout integrity

- sidebars remain in their intended columns
- main content is not pushed, overlapped, or squeezed unexpectedly
- cards/panels remain aligned with their grid or column
- menus remain in their expected flow and hierarchy
- new components are inserted into the correct parent container
- headers, footers, sticky bars, and page actions do not collide with content
- vertical and horizontal spacing remains intentional rather than accidental
- the page hierarchy remains understandable at a glance

### Overflow and clipping

- no horizontal page scroll unless explicitly intended
- buttons do not protrude outside cards, table cells, panels, or sidebars
- labels do not collide with buttons, toggles, badges, or inputs
- long text wraps, truncates, or scrolls according to the intended design
- tables do not exceed their container unexpectedly
- badges, counts, pills, and status labels fit their parent row
- dropdowns, tooltips, and modals are not clipped by parent overflow
- images/media stay within their bounds and reserve dimensions to avoid layout shift

### Controls and forms

- buttons fit their containers at all tested widths
- button text remains readable
- disabled, loading, hover, focus, and active states still look intentional
- labels remain associated with their controls and are readable
- inputs/search boxes do not overlap table headers, icons, or actions
- pagination remains visible and usable
- controls have enough spacing to avoid accidental clicks/taps
- mobile touch targets are large enough for practical use, commonly around 44px minimum

### Readability and contrast

Use WCAG 2.2 contrast as the practical baseline when code/computed colors are available:

| Element | Minimum contrast |
|---|---:|
| Normal text and images of text | 4.5:1 |
| Large text, roughly 18pt+ or 14pt+ bold | 3:1 |
| Enhanced target for normal text when practical | 7:1 |
| Enhanced target for large text when practical | 4.5:1 |
| Meaningful non-text UI indicators, control borders, focus rings, and icons | 3:1 |

Screenshot inspection should flag likely contrast failures even before exact ratios are calculated. If the screenshot shows faint gray text on white, low-contrast button text, barely visible close icons, or instructional copy that looks disabled, treat it as a visual QA failure until verified or fixed.

Rules:

- Required instructions, labels, confirmation text, dialog copy, and error/help text must remain readable; do not style them as disabled or decorative.
- Disabled controls may be visually muted, but users must still understand why an action is unavailable and what to do next.
- Do not rely on color alone to communicate disabled, error, success, warning, selected, or required states; include text, iconography, shape, position, or other cues.
- Control outlines, checkbox/radio borders, focus indicators, icons, and status marks must be distinguishable from adjacent colors.
- If exact colors are available, calculate contrast instead of guessing. If only screenshots are available, make a conservative judgment and call out uncertainty.

Example failures this skill must catch:

- modal body text is light gray on white and appears unreadable
- required checkbox label looks disabled even though the user must read and accept it
- primary button text has weak contrast against its background
- close icon or focus ring is too faint to discover
- error/success/warning state is communicated only by color

### Dialog action flow and control placement

There is no single universal left/right button-order rule across all platforms. Material, Microsoft, Apple, GOV.UK, Bootstrap, and product-specific design systems differ. The enforceable rule is consistency and logical action flow.

Check that dialog/page actions:

- are grouped in one predictable action area, commonly a modal footer or form action row
- keep primary and secondary/dismissive actions visually related, not scattered across unrelated corners or rows
- follow the product's existing design-system convention for order, alignment, emphasis, and destructive actions
- place the primary action where the user's reading/task flow naturally ends
- keep disabled primary actions in the same expected location they will occupy when enabled
- keep Cancel/Close/Dismiss near the primary action or in a clearly understood dismiss location
- use concise, outcome-specific labels such as "Save changes", "Delete user", or "Continue" instead of ambiguous labels like "Yes" when context is not obvious
- avoid multiple competing primary buttons in the same decision area
- distinguish destructive or irreversible actions with explicit wording and confirmation; do not rely on red color alone
- include a visible safe/nondestructive escape action for dialogs, such as Cancel, Close, or a close icon

Example failures this skill must catch:

- Cancel and Continue are separated into different footer regions, making the decision flow unclear
- a disabled Continue button is detached from the confirmation checkbox and expected action row
- a primary action appears before the user has encountered required instructions or choices
- secondary actions visually overpower the primary action
- destructive action sits where a safe default is expected without confirmation or clear wording

### UX flow

- users can still find the same navigation paths after the change
- added controls appear near the content they affect
- existing menus are not reordered, displaced, or visually demoted unless requested
- the change does not make the user scan unrelated page regions to complete the same task
- disabled or unavailable actions communicate their state clearly
- success, error, warning, and empty states remain visible and understandable

## Regression Pattern: Sidebar/Menu Pushed Into Page

A high-priority failure pattern is adding a new card/control group to a side area and accidentally breaking the page layout.

Example failure this skill must catch:

- baseline: a stable two-column page with a left sidebar and a main content area
- edit: a new settings/preferences card is inserted into the left area
- regression: the new card consumes too much width or is placed at the wrong layout level
- visible symptoms:
  - buttons overflow horizontally outside the card
  - labels wrap awkwardly or collide with buttons
  - a left-side menu is pushed into the main page area
  - the main content alignment no longer matches the baseline
  - the page technically loads, but the UX flow is broken

Required response:

1. Mark visual QA as failed.
2. Do not claim completion.
3. Fix the component placement, width behavior, and wrapping.
4. Re-capture final screenshots.
5. Compare against the baseline again.

## Responsive Viewport Requirements

Use user-specified dimensions when provided. Otherwise test a practical minimum set:

| Class | Viewport | Purpose |
|---|---:|---|
| Narrow mobile | 320x568 | catches worst-case overflow and cramped controls |
| Standard mobile | 390x844 | common modern phone width |
| Tablet portrait | 768x1024 | catches sidebar/tablet breakpoint issues |
| Desktop | 1366x768 | common working desktop viewport |
| Wide desktop | 1920x1080 | catches max-width, spacing, and wide layout issues |

When practical, drag-resize or test intermediate widths instead of only jumping between breakpoints. Many layout failures happen between standard breakpoints, such as 430-767px, 769-1023px, or 1024-1365px.

For each viewport, check:

- no unintended horizontal scroll
- no clipped content
- no overlapping panels or controls
- no off-screen primary actions
- readable typography
- usable menus/navigation
- appropriate stacking/wrapping behavior

## Common CSS/Layout Failure Patterns

Actively look for these during review and debugging:

1. **Missing `min-width: 0` in flex/grid children** — long text or buttons force the child wider than its container.
2. **Fixed-width controls in narrow containers** — buttons or labels overflow sidebars/cards.
3. **Labels and controls forced into one row** — looks acceptable at one width, breaks at another; use wrapping, stacking, or grid constraints.
4. **Component inserted at the wrong layout level** — a sidebar/menu/card becomes a sibling of main content instead of a child of the intended column.
5. **Hiding overflow to mask layout breakage** — `overflow: hidden` may conceal content and create UX failures; fix sizing/wrapping first.
6. **Desktop-only spacing** — works at 1366px but fails on tablet/mobile.
7. **Mobile-only fix that damages desktop** — verify both directions.
8. **Unbounded tables or long strings** — IDs, emails, hostnames, filenames, URLs, and status text can break layout.
9. **Image/media dimensions omitted** — layout shifts or overflows after loading.
10. **Sticky/fixed elements inside transformed or overflowed parents** — positioning breaks unexpectedly.
11. **Z-index escalation** — dropdowns/modals/tooltips appear behind cards or sticky headers.
12. **100vh on mobile** — browser chrome and keyboard can cause clipped content; prefer modern viewport units where appropriate.
13. **Duplicated desktop/mobile navigation** — hidden duplicate controls drift out of sync or confuse keyboard/screen-reader flow.
14. **Testing only the changed component** — misses surrounding page regressions.
15. **Low-contrast required text** — light gray text on white may make instructions, labels, or confirmation copy unreadable even though the component is technically present.
16. **Disabled-looking content that is required to act** — required terms, labels, helper text, or modal instructions inherit disabled styling and users cannot tell what matters.
17. **Scattered dialog actions** — Cancel, Continue, Save, Delete, or Back controls appear in unrelated footer regions or corners instead of a coherent action group.
18. **Unstable disabled action placement** — a disabled primary action is placed somewhere different from where the enabled primary action would appear, breaking user expectation.
19. **Ambiguous button labels/order** — generic labels or inconsistent ordering make the consequence of a click unclear.
20. **Color-only state cues** — disabled/error/success/warning/selected state depends only on color and lacks text, icon, shape, or placement support.

## Browser and Screenshot Workflow

Use whatever browser, screenshot, or vision tools are available in the environment. The workflow is more important than a specific tool.

Preferred loop:

1. Start or open the app/page.
2. Set viewport to the target dimensions.
3. Navigate to the exact UI state.
4. Capture baseline screenshot before edits for existing UI.
5. Implement the change.
6. Capture final screenshot at the same state/dimensions.
7. Compare visual claims and baseline stability.
8. Fix failures and repeat until passing or blocked.

If using automated browser tests, include screenshot artifacts on failure and consider asserting absence of horizontal scroll:

```js
expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
```

Also consider checking that important controls are inside their parent bounds:

```js
const fits = await page.evaluate(() => {
  const parent = document.querySelector('[data-testid="panel"]')?.getBoundingClientRect();
  const child = document.querySelector('[data-testid="primary-action"]')?.getBoundingClientRect();
  if (!parent || !child) return false;
  return child.left >= parent.left && child.right <= parent.right && child.top >= parent.top && child.bottom <= parent.bottom;
});
expect(fits).toBe(true);
```

These checks complement screenshots; they do not replace visual inspection.

## Handling User-Provided Screenshots

When the user attaches screenshots:

- Use vision/image analysis tools to inspect them.
- Determine whether each screenshot is baseline, target, regression, mockup, or evidence.
- Extract concrete visual claims from the screenshots.
- If a screenshot shows a bad state, encode the failure pattern and verify the final UI no longer shows it.
- If the user provides both original and post-edit screenshots, compare layout relationships, not just individual components.

For screenshot pairs, explicitly compare:

- column/sidebar/main-content boundaries
- component order and placement
- control bounds and text wrapping
- menu/navigation position
- spacing and alignment
- whether the new/edited component changed unrelated page regions

## Failure Handling

If visual QA fails:

1. Stop and mark the UI as not acceptable yet.
2. Name the exact visible failure, e.g. "button extends outside parent card at 1366px" or "left menu moved into main content after preferences card was added."
3. Identify likely layout cause: wrong parent, fixed width, flex min-width, missing wrap, grid column issue, overflow masking, etc.
4. Fix narrowly.
5. Re-render and recapture screenshots.
6. Re-run the failed visual claims.
7. Only finalize when the rendered UI passes or when a blocker is honestly reported.

Never use language like "should be fixed" for UI deliverables unless the rendered final state was checked. Say what was actually verified.

## Final Response Requirements

For UI work, the final response must include a compact Visual QA section.

For existing UI revisions:

```md
Visual QA:
- Baseline captured before edits: yes/no, path or note
- Final screenshots: path(s) or note
- Viewports checked: 1366x768, 390x844, ...
- Compared against baseline: yes/no
- Claims:
  - PASS: <stable layout claim>
  - PASS: <expected change claim>
  - FAIL/BLOCKED: <issue if any>
- Result: visually acceptable / not acceptable / blocked with reason
```

For new UI:

```md
Visual QA:
- Screenshots: path(s) or note
- Viewports checked: ...
- Claims:
  - PASS: <visible claim>
  - PASS: <responsive claim>
- Result: visually acceptable / not acceptable / blocked with reason
```

If browser/screenshot verification was not possible, say why and do not overstate confidence.

## Common Pitfalls

1. **Starting existing UI edits without a baseline screenshot.** This destroys the ability to distinguish intentional changes from regressions. Capture baseline first.
2. **Checking only the new component.** Surrounding menus, sidebars, tables, and page actions are often what break.
3. **Assuming page load equals visual success.** A page can load with overflowing buttons, clipped labels, and broken navigation.
4. **Testing only one desktop width.** Sidebar and table bugs often appear at tablet widths or narrow desktops.
5. **Ignoring disabled/empty/error/long-text states.** These states often have different labels, button widths, or wrapping behavior.
6. **Using `overflow: hidden` as a fix.** It may hide the symptom while making controls unreachable.
7. **Forgetting existing UX flow.** New controls should not displace unrelated existing navigation or change how users scan the page unless requested.
8. **Claiming success from code inspection.** UI success requires rendered verification.
9. **Not reporting evidence.** If screenshots or viewport checks were performed, include them; if not, state the limitation.

## Verification Checklist

Before finalizing any UI task, confirm:

- [ ] For existing UI, baseline screenshots were captured before edits or a limitation was stated.
- [ ] Expected visible changes were defined.
- [ ] Stable existing layout relationships were defined.
- [ ] Final screenshots were captured after edits.
- [ ] Baseline and final screenshots were compared for existing UI.
- [ ] Desktop and mobile/tablet viewports were checked or explicitly scoped out.
- [ ] No unintended horizontal scroll exists.
- [ ] Buttons, labels, badges, inputs, and menus fit inside their containers.
- [ ] Sidebars, menus, and main content remain in the correct layout flow.
- [ ] Text is readable and not clipped, awkwardly wrapped, or too low-contrast for its background.
- [ ] Required labels, instructions, and modal copy do not look disabled unless truly inactive.
- [ ] Meaningful icons, control borders, focus rings, and state indicators are visible against adjacent colors.
- [ ] Color is not the only signal for disabled, error, warning, success, selected, or required states.
- [ ] Dialog/page actions are grouped in a predictable action area and follow the product's control-flow convention.
- [ ] Primary, secondary, cancel, close, disabled, and destructive actions are placed logically and labeled by outcome.
- [ ] Touch/click targets remain usable.
- [ ] Empty, loading, disabled, error, and long-content states were considered when relevant.
- [ ] Final response includes visual QA evidence and any deviations/blockers.
