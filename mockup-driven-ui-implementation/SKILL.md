---
name: mockup-driven-ui-implementation
description: Use when converting a screenshot, mockup, design target, or visual reference into standalone pixel-accurate HTML/CSS. Produces a self-contained static implementation, infers exact layout/spacing/type/color/assets from the image, uses image editing or generation when needed for visual fidelity, and iterates in the browser until the standalone result matches.
version: 1.2.0
author: rs-radic
license: MIT
platforms: [linux, macos, windows]
tags: [ui, mockups, image-to-html, standalone-html, visual-implementation, screenshots, image-to-image, frontend]
related_skills: [ui-deliverable-visual-qa]
---

# Mockup-Driven UI Implementation

## Overview

Use this skill when the goal is to create standalone HTML/CSS that matches a screenshot, mockup, generated design, visual comp, or reference image as closely as possible. The standalone artifact is the deliverable.

The purpose is to get to near-perfect HTML/CSS from the image. The mockup is the visual source of truth. Do not treat it as inspiration, simplify difficult regions, or substitute unrelated design ideas unless the user explicitly accepts that visual deviation.

Always render the standalone result in a browser and compare it against the target image before calling it done.

## When to Use

Use when the request includes any of these:

- "turn this mockup into HTML"
- "create standalone HTML from this screenshot"
- "make this as close as possible"
- "pixel-perfect HTML/CSS"
- "build the mockup"
- "convert this image/design to markup"
- "the standalone result does not match the mockup"
- visual corrections about spacing, type, cards, images, art, checkmarks, bullets, colors, clipping, or proportions

Do not use this skill for:

- backend-only work
- broad design ideation without a concrete visual target
- final visual QA after a standalone artifact already exists; use `ui-deliverable-visual-qa`

## Core Principle

> Build standalone HTML/CSS that matches the visual target.

Default to self-contained, browser-openable files. The work is not complete after the first generated file; completion requires rendered comparison and iteration.

## 1. Confirm the Standalone Contract

Before writing code, confirm only the details that materially affect the standalone artifact. Do not ask the user for measurements, colors, or spacing; infer those from the image.

Confirm when not obvious:

- Which screenshot/mockup is the target.
- Whether the output should be a single `index.html` file or separate `index.html` + `styles.css`.
- Whether vanilla HTML/CSS is required. Default is vanilla HTML/CSS.
- Whether JavaScript is allowed for visible interactions; default is no JavaScript unless needed for a mockup-visible state.
- Which viewport is the primary reference if the mockup could be desktop, tablet, or mobile.
- Which visible regions are in scope if the image includes browser chrome, unrelated surroundings, or multiple screens.
- Whether generated/edited images may be used to reproduce visual assets more closely.

Completion criteria:

- The target image is known.
- The output format is clear or defaulted to standalone `index.html` with embedded CSS.
- The primary reference viewport is chosen or inferred.
- In-scope visual regions are clear enough to build without guessing product intent.

## 2. Analyze the Image Before Coding

Inspect the image visually and turn it into a design extraction note. The note can live in comments, a temporary scratch file, or the final response.

Extract:

- Structure: header, hero, sections, cards, sidebars, tables, lists, forms, footer, modal, or dashboard areas.
- Hierarchy: primary heading, secondary headings, body copy, captions, labels, badges, buttons, links.
- Reference viewport: width/height inferred from the image, such as 1440px desktop or 390px mobile.
- Grid: page max width, columns, gutters, card widths, alignment boundaries, left/right margins.
- Spacing: section padding, card padding, margins, gaps, list spacing, button padding.
- Typography: font family guess, sizes, weights, line heights, letter spacing, casing.
- Colors: exact hex/rgba values for background, text, muted text, borders, accents, buttons, and shadows.
- Effects: border radius, border width/color, box shadow, gradients, opacity, blur, overlays.
- Assets: icons, illustrations, logos, screenshots, device frames, charts, maps, decorative art.
- State: selected/active/disabled/hover-like visual states shown in the mockup.

Use a consistent measurement scale. Prefer 4px or 8px increments, but use exact values when the design clearly requires them. Avoid arbitrary guesses unless they are deliberate approximations from the reference.

Completion criteria:

- A reference viewport is recorded.
- The main layout grid and spacing scale are known.
- Type/color/effect tokens are extracted.
- Asset requirements are identified before the first build pass.

## 3. Set Canvas and Viewport Deliberately

Distinguish the design canvas from the browser viewport.

- **Design canvas** is the size represented by the mockup image, such as 1440x900.
- **Browser viewport** is the screenshot size used to render/check the standalone file, such as 1440x900 or 1920x1080.
- If the target is fixed-width, explicitly set the page/canvas width and center it so the render does not stretch.
- If the target is responsive, still match the reference canvas first, then add responsive rules after the reference view is close.
- Include `<meta name="viewport" content="width=device-width, initial-scale=1.0">` unless the user explicitly asks for a fixed exported canvas without mobile scaling.

Useful starting reset:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  line-height: 1.5;
}
```

Completion criteria:

- The reference canvas dimensions are recorded in the HTML/CSS notes.
- The browser screenshot viewport is recorded separately.
- The render is not squished, stretched, unexpectedly centered, or unexpectedly full-width.

## 4. Generate Standalone HTML/CSS Only by Default

Default output is self-contained vanilla HTML and CSS that can be opened directly in a browser.

Rules:

- Use semantic HTML: `header`, `main`, `section`, `article`, `nav`, `aside`, `footer`, lists, headings, buttons, and forms where visually appropriate.
- Use plain CSS. No Tailwind, Bootstrap, Material, React, Vue, Svelte, Angular, Sass, build tools, or package installs unless the user requests them.
- Prefer one `index.html` with embedded CSS for fastest visual iteration unless the user asks for separate files.
- Use CSS custom properties for extracted colors, spacing, radii, shadows, and type scale when useful.
- Include a design-notes comment with the reference viewport and key extracted values.
- Use one-to-one visible region mapping: the markup structure should mirror the visual regions in the mockup.
- Do not include TODO colors, placeholder dimensions, lorem ipsum, fake icons, or temporary generated art in the final unless visible in the target or approved.
- Use `alt` text for meaningful images. Use empty `alt=""` only for decorative images.

Completion criteria:

- The artifact opens without a build step.
- The DOM structure corresponds to the visible mockup structure.
- CSS values are concrete and derived from the image.
- There are no unapproved placeholders or framework dependencies.

## 5. Pixel-Accuracy Discipline

Implement with measurement and visual matching first, not generic design taste.

Follow this order:

1. Layout and grid: page width, columns, card widths, section boundaries.
2. Spacing: padding, margins, gaps, list spacing, button spacing.
3. Typography: font, size, weight, line-height, letter spacing, color.
4. Shape: border radius, borders, shadows, dividers, backgrounds.
5. Assets: icon/art placement, image size, crop, transparency, object fit.
6. States: selected, disabled, active, highlighted, warning, success, or error styling.
7. Responsive behavior if the mockup or user requires more than one viewport.

Be specific:

- Use `font-weight: 400/500/600/700`, not generic `bold` when matching visual weight.
- Use actual hex or rgba values, not named colors like `gray` or `blue`.
- Use explicit dimensions/aspect ratios for cards, art, and media where the mockup implies them.
- Use flex/grid intentionally with concrete `gap`, `align-items`, `justify-content`, `grid-template-columns`, and max-width values.
- If a shape is hand-drawn by CSS, compare it visually in the browser, not just by reading code.

Completion criteria:

- The first render resembles the mockup structurally before detail iteration begins.
- The largest visible differences are known and prioritized.
- Every correction changes an observed visual mismatch.

## 6. Asset and Image Handling

Use the asset path that gets closest to the mockup.

Asset separation decision matrix:

| Visible asset type | Prefer | Use when |
|---|---|---|
| simple geometric icon | CSS or inline SVG | one-color or few-color shapes, checkmarks, dots, arrows |
| complex icon/logo | SVG file or inline SVG | vector detail must stay crisp at any scale |
| photo/product image | image file | photographic or screenshot-like content |
| simple background | CSS color/gradient | solid fills, linear/radial gradients, simple overlays |
| texture/noise/complex illustration | image file or generated/edited image | CSS/SVG would be slower or less faithful |
| tiny repeated mark | CSS pseudo-element or inline SVG | bullets, checkmarks, separators, repeated ornaments |

Preferred options:

1. Crop clean assets from the provided mockup when exact fidelity is desired and allowed.
2. Use image-to-image generation or image editing to produce a closer asset from the mockup/reference.
3. Use existing provided assets if they match the target.
4. Draw simple assets with CSS/SVG when that is more controllable than generation.
5. Use text-to-image generation only when no adequate source/reference asset exists.

Rules:

- Do not use generated art blindly. Place it into the standalone HTML and compare it against the mockup.
- If generated art has clipped corners, random text, watermarks, extra UI labels, wrong perspective, wrong color, or incorrect composition, regenerate/edit/replace it.
- If the issue is CSS sizing or clipping, fix CSS/container/object-fit instead of regenerating the image.
- Match the target asset's crop, placement, opacity, and scale in context.
- Keep final asset filenames descriptive and remove unused generated attempts from the deliverable folder.

Completion criteria:

- All visible assets are either reproduced, cropped, generated/edited, or intentionally replaced.
- Generated/edited assets are verified in the rendered page.
- No accidental text artifacts, watermarks, or clipped source images remain.

## 7. Render and Compare in a Browser

The task is not complete after writing HTML/CSS. Open the artifact in a browser or browser automation and compare it to the mockup.

Capture:

- screenshot of the standalone render at the reference viewport
- the target/mockup image or crop
- side-by-side comparison or clearly paired screenshots
- additional viewport screenshots if responsive behavior is in scope
- browser console errors if the preview environment exposes them

Compare:

- section boundaries and order
- alignment grid and gutters
- card dimensions and positions
- text size/weight/color/line breaks
- button sizes and placement
- border radius, shadows, backgrounds, gradients
- icons/art size, crop, placement, and clipping
- color contrast and readability
- page height and scroll behavior

Fix visual errors in priority order:

1. **Critical:** layout structure, major positioning, canvas size, section order.
2. **High:** typography size/weight/line-height, major spacing, image scale/crop.
3. **Medium:** colors, borders, shadows, radius, gradients, opacity.
4. **Low:** tiny alignment differences, subtle anti-aliasing, minor gradient nuance.

Screenshot early and often. A full-page screenshot can hide viewport-only problems, so capture viewport-sized evidence when viewport fidelity matters.

Completion criteria:

- The rendered standalone page has been visually compared to the target.
- The comparison uses fresh screenshots after the latest edits.
- Remaining mismatches are listed, fixed, or explicitly accepted.

## 8. Iterate Until the Standalone Matches

Use a tight visual loop. Do not move on while the standalone artifact still visibly drifts from the mockup.

Correction order:

1. Spacing: padding, margin, gap, section height.
2. Alignment: grid, flex, column width, text alignment, object position.
3. Typography: size, weight, line-height, font family, letter spacing.
4. Colors/effects: hex values, borders, shadows, gradients, opacity.
5. Proportions: card sizes, image sizes, icon sizes, aspect ratios.
6. Assets: crop, generated/edited image quality, clipping, artifacts.
7. Responsive adjustments: only after the reference viewport is close.

If the user reports a mismatch, reproduce it in the browser, fix that specific visual difference, and capture a new comparison.

Completion criteria:

- User-called-out mismatches have pass/fail status.
- Corrections are based on screenshot evidence, not vague preference.
- The final standalone artifact is visually close to the target.

## 9. Responsive Translation

If the mockup only shows one viewport, match that viewport first. Do not invent multiple responsive layouts before the reference is correct.

When responsive output is requested or implied:

- preserve the visual hierarchy from the reference
- keep content inside the viewport
- stack sections predictably
- maintain spacing rhythm and readable type
- keep assets within their intended containers
- avoid horizontal page scroll

Minimum practical widths when responsive behavior matters:

- the mockup/reference width
- 390px mobile
- one intermediate width if layout changes between desktop and mobile

Completion criteria:

- Reference viewport match is established first.
- Responsive adaptations are checked with screenshots.
- No unintended horizontal overflow or clipped content remains.

## Final Response Requirements

For a completed standalone mockup build, include:

```md
Standalone mockup build:
- Target/mockup: <path or description>
- Output: <index.html/styles/assets paths>
- Reference viewport: <width>x<height>
- Rendered screenshots: <paths>
- Standalone-vs-mockup comparison: <paths>
- User-called-out details:
  - PASS/FAIL: <detail>
  - PASS/FAIL: <detail>
- Remaining drift/accepted deviations: <none or list>
```

Do not bury visual drift in a generic success message. If anything remains off, say so.

## Common Pitfalls

1. **Treating the mockup as inspiration.** Match the image closely unless the user approves a deviation.
2. **Asking for measurements instead of inferring them.** Infer viewport, spacing, colors, and typography from the image.
3. **Using frameworks by default.** Vanilla HTML/CSS is the default unless requested otherwise.
4. **Stopping after first generation.** Render, compare, and iterate.
5. **Using placeholder visuals.** TODO colors, lorem ipsum, fake icons, and temporary assets are not acceptable final output.
6. **Ignoring asset defects.** Clipped/generated/watermarked/wrong-perspective art must be replaced or edited.
7. **Comparing stale screenshots.** Use fresh screenshots after every meaningful change.
8. **Overbuilding responsiveness before matching the reference viewport.** Match the source first, then translate.
9. **Letting generated assets drive the design.** The target image drives the design; generated assets must conform to it.
10. **Claiming visual match without browser evidence.** Code inspection is not proof; use rendered screenshots.

## Verification Checklist

Before finalizing, confirm:

- [ ] Target image/mockup was inspected visually.
- [ ] Output is standalone HTML/CSS unless the user requested otherwise.
- [ ] Reference viewport is recorded.
- [ ] Design extraction includes grid, spacing, typography, colors, effects, and assets.
- [ ] HTML structure maps to visible mockup regions.
- [ ] CSS uses concrete values derived from the mockup.
- [ ] No unapproved frameworks, build tools, placeholders, TODO visuals, or lorem ipsum remain.
- [ ] Assets are cropped/generated/edited/drawn intentionally.
- [ ] Generated or edited assets were rendered in context and compared.
- [ ] Browser screenshot of the standalone render was captured.
- [ ] Rendered screenshot was compared against the mockup.
- [ ] User-called-out mismatches were fixed or explicitly accepted.
- [ ] Responsive screenshots were captured if responsive behavior is in scope.
- [ ] Remaining drift is documented honestly.
