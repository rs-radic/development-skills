---
name: documentation-video-walkthrough
description: Use when creating product documentation and narrated explainer videos from UI screenshots. Builds separate DOCX and video artifacts with safe UI capture, feature-focused video highlights, clean documentation crops, artifact-specific writing, and mandatory visual QA before delivery.
version: 1.0.0
author: rs-radic
license: MIT
tags: [documentation, video, docx, manim, screenshots, visual-qa, training]
related_skills: [manim-video, word-docx, ui-deliverable-visual-qa]
---

# Documentation + Video Walkthrough

## Overview

Use this skill to create polished product walkthrough deliverables from UI screenshots: a narrated explainer video, a written DOCX guide, or both. The core discipline is that **documentation screenshots and video frames are different artifacts**.

Documentation can use overview screenshots, individual crops, compact callouts, and explanatory text. Video should usually show one focused feature at a time, synchronized with narration, so viewers can connect what they hear to the one visible control being highlighted.

This skill orchestrates existing specialist skills rather than replacing them:

- Use `manim-video` for animation, timing, rendering, and MP4 production.
- Use `Word / DOCX` for Word package structure, embedded images, and document validation.
- Use `ui-deliverable-visual-qa` for visual inspection of final screenshots, crops, frames, and layout.

## When to Use

Use when the user asks for any of these from a UI or product workflow:

- customer-facing or admin-facing documentation
- a narrated explainer/tutorial/walkthrough video
- a DOCX guide with screenshots
- a training handoff for an internal operations, support, billing, or admin team
- a combined media package: video + written guide
- a screenshot-based explanation of how to use an interface

Do not use for:

- pure code documentation without screenshots or media
- marketing videos that do not explain an interface workflow
- live demos that require state-changing actions unless the user explicitly approves those actions
- formal API reference documentation; use a docs/API-specific workflow instead

## Inputs

Gather or confirm these inputs before building artifacts:

- product/interface screenshots
- company logo or brand asset, if branding is requested
- desired voice/tone, if narration is requested
- target output formats: MP4, DOCX, PDF, HTML, or a combination
- workflow boundaries: what the user wants explained and what is out of scope
- safety constraints: actions that must not be performed, such as submit, delete, assign, unassign, purchase, charge, send, publish, or provision

Completion criteria:

- Every required source file exists or a missing-asset limitation is explicitly stated.
- State-changing actions are identified and avoided unless explicitly approved.
- Screenshots are sufficient to explain the workflow, or safe navigation-only capture is planned.

## Safety Rules

1. **Default to screenshot-only demonstrations.** Do not perform destructive or state-changing product actions just to create documentation or video.
2. **Safe navigation is allowed.** It is usually acceptable to click tabs, open read-only views, use filters, search, paginate, or capture screenshots, as long as those actions do not change product state.
3. **State-changing actions require explicit approval.** Examples: assign, unassign, delete, submit, send, charge, provision, cancel, suspend, save, publish, or commit.
4. **Do not imply live actions were performed if they were not.** If the artifact explains an action using screenshots only, write the guide as instructions, not as a claim that the action was demonstrated live.
5. **Keep final artifacts end-user-facing.** Do not include internal process notes, revision history, tool failures, or conversation-specific wording.

Completion criteria:

- The plan identifies any unsafe actions.
- The delivered artifacts do not require unapproved state changes.
- The final response states any relevant limitation without putting that limitation into the customer-facing artifact unless appropriate.

## Artifact Split: DOCX vs Video

### DOCX documentation

Written documentation is best for reference. It can show more context and include explanatory text outside the screenshot.

Use:

- unannotated overview screenshots
- individual control crops
- compact numbered markers only when they improve orientation
- explanations in document text, not long labels over the UI
- sequential sections: confirm context, choose workflow, search/filter, select/review, submit, verify result

Avoid:

- long labels drawn inside screenshots
- callout boxes that cover the control being explained
- many overlapping highlight boxes
- screenshots where the crop does not fully cover the intended control
- relying only on a full-screen screenshot when a small crop would be clearer

### Video walkthrough

Video is best for guided attention. The viewer should see one obvious focus target at the same time the narration discusses that feature.

Use:

- one feature or logical group per scene
- dimmed full-screen screenshot with the active feature restored/brightened
- one visible highlight around the active feature
- neutral caption bars that do not look like additional highlight boxes
- animated focus transitions between features
- per-segment narration timing

Avoid:

- reusing documentation screenshots with many callouts for feature-specific narration
- multiple unrelated highlights while the voice discusses one feature
- captions overlapping the highlighted feature
- colored caption borders that look like a second highlight box
- special glyphs that may render as missing-character squares
- blank ending frames

## Workflow

### 1. Inventory assets

Find or create a working directory for the deliverables. Inventory screenshots, logo files, existing videos, and previous documentation.

Completion criteria:

- Source screenshot paths are known.
- Logo path is known or explicitly unavailable.
- Output directory is created.
- Existing artifacts that may be superseded are identified.

### 2. Define the visual contract

Write a short plan before generating files:

- What workflow is being taught?
- Which controls/features need explanation?
- Which screenshot is the overview?
- Which controls need individual crops?
- Which scenes belong in the video?
- Which actions are safe to show directly?
- Which actions should only be described?

Completion criteria:

- Video scenes map one-to-one to narration segments or logical feature groups.
- DOCX sections map to screenshots/crops.
- Unsafe or state-changing actions are marked as do-not-perform unless approved.

### 3. Prepare screenshots and crops

Create separate image assets for documentation and video.

For DOCX:

- Keep overview screenshots unannotated when possible.
- Create individual crops for important controls.
- Add borders outside crops if visual emphasis is needed.
- If numbered callouts are necessary, keep markers compact and put descriptions in document text.

For video:

- Use original unannotated screenshots as the base.
- Store exact pixel coordinates for each feature box.
- Generate focus crops from those coordinates.
- Use the coordinates to place highlight boxes in the rendered frame.

Completion criteria:

- Every crop fully contains the intended control.
- No marker or label covers the meaningful part of the UI.
- Feature coordinates are saved in a reusable file or data structure.

### 4. Write end-user-facing narration and documentation text

Keep copy direct, procedural, and reusable.

Good examples:

```text
Start by confirming the selected server.
Use the search field to narrow the list before selecting rows.
After submitting, review the counts and row list to confirm the expected result.
```

Avoid meta or conversation-specific language:

```text
This updated video shows...
As requested...
No records were changed while making this video...
Here is the revised version...
```

Completion criteria:

- Narration contains no revision-history or conversation-specific wording.
- Documentation text explains actions without overclaiming live demonstration.
- The company/audience/product names are generic or supplied by the user.

### 5. Generate per-segment narration audio

For narrated videos, generate one audio file per scene or narration segment. Measure each audio duration and use those measured durations to drive the Manim visual timing.

Recommended pattern:

1. Create `segments.json` with `id`, `caption`, `voice`, `image`, and feature box data.
2. Generate `audio/<index>_<id>.mp3` per segment.
3. Measure each segment duration with MoviePy or ffprobe.
4. Write `segments_timed.json` including `audio_duration` and `visual_duration`.
5. Render Manim using the timed segment file.

Completion criteria:

- Every narration segment has an audio file.
- Every visual scene duration comes from measured audio duration plus intentional padding.
- Final visual/audio drift is measured before muxing.

### 6. Render the video

Use Manim for the visual track. A reliable scene pattern is:

- title/brand intro
- repeated feature-focus scenes:
  - full screenshot
  - dim overlay
  - bright crop of active feature
  - one highlight outline around active feature
  - neutral caption away from feature
  - short pulse animation on the highlight
- final recap scene with simple text and glyph-safe bullets/dots

Completion criteria:

- MP4 renders successfully.
- Audio is muxed into final MP4.
- Final duration and audio presence are verified.
- A compressed copy is produced if the delivery channel has size limits.

### 7. Build the DOCX

Use the Word/DOCX skill. If generating directly as OOXML:

- include valid `[Content_Types].xml`
- include `_rels/.rels`
- include `word/document.xml`
- include `word/_rels/document.xml.rels`
- embed images under `word/media/`
- use named paragraph styles when practical
- set page size/margins explicitly

Recommended document structure:

1. Title and short purpose statement
2. Interface overview
3. Confirm context/current state
4. Choose workflow/action
5. Search/filter
6. Review/select rows
7. Submit action, if applicable
8. Verify result
9. Safety checklist or final verification

Completion criteria:

- DOCX opens as a valid ZIP package.
- `word/document.xml` exists.
- Image relationships resolve.
- Expected image count is present.
- Extracted document text contains the title and major sections.

### 8. Validate structure

Run structural validation before visual QA.

For video:

- file exists
- size is known
- duration is known
- resolution is known
- audio track exists
- compressed delivery copy exists if needed

For DOCX:

- file exists
- package structure is valid
- embedded image count is correct
- text extracts cleanly
- expected phrases/sections are present
- unwanted phrases are absent

Completion criteria:

- Structural validation results are recorded.
- Any missing audio, missing image, invalid package, or unexpected phrase is fixed before visual QA.

### 9. Perform visual QA on exact deliverables

This step is mandatory. Do not hand off artifacts based only on successful rendering or package validation.

For video:

1. Extract frames from the exact final MP4 that will be delivered.
2. Build a montage covering every scene or at least every feature type.
3. Visually inspect for:
   - one focus target per feature scene
   - highlight boxes aligned to controls
   - captions not overlapping highlights
   - no extra colored boxes that look like highlights
   - no missing-glyph boxes or weird numbered bullets
   - no blank ending
   - readable text after compression
   - visible branding if requested
   - no conversation-specific wording

For DOCX:

1. Build a montage of the exact screenshot/crop assets embedded in the DOCX.
2. Visually inspect for:
   - crops fully covering intended controls
   - no overlay labels covering UI content
   - search/filter/action/pagination controls framed correctly
   - readable screenshots
   - professional spacing and visual hierarchy

Completion criteria:

- The exact delivered MP4 was visually reviewed.
- The exact delivered DOCX screenshot/crop set was visually reviewed.
- Must-fix issues were corrected and re-reviewed.

### 10. Deliver final artifacts

Attach the final files and summarize validation compactly.

Mention:

- output paths/files
- video duration, resolution, audio presence, and size
- DOCX structure validation
- visual QA result
- any limitations, such as missing logo or unavailable live capture

Do not include internal false starts, failed intermediate renders, or revision criticism in the customer-facing artifacts.

## Coordinate and Highlight Rules

When drawing highlights from screenshots:

- Store feature boxes as pixel coordinates against the original screenshot.
- Use the same original image dimensions when converting to video coordinates.
- Add a small padding only if it does not include unrelated UI.
- For form controls, cover the entire input/select/button, not just its label or left edge.
- For action buttons, cover the complete button including text and padding.
- For pagination, avoid accidentally including unrelated badges or row content above the controls.
- For row-level features, crop enough context to show the table row but not so much that the target becomes unclear.

Completion criteria:

- Candidate coordinate montage has been visually checked before final render.
- Any previously criticized or high-risk controls get explicit visual review.

## Narration and Copy Rules

Use a calm, practical, end-user tone. Write for the actual user of the interface without naming the audience unless requested.

Prefer:

- imperative workflow guidance: “Confirm…”, “Use…”, “Review…”
- short sentences
- concrete UI labels
- safety checks before state-changing actions
- final verification steps

Avoid:

- internal labels like “billing guys” unless the user specifically wants them in the artifact
- implementation details irrelevant to use
- “this video”, “this updated guide”, or “as requested”
- claims about actions taken during production
- ambiguous button labels in prose when the UI has specific labels

Completion criteria:

- Copy reads like final documentation, not a chat response.
- Internal process notes are absent from artifacts.

## Common Pitfalls

1. **Reusing DOCX callout screenshots in video.** Static documentation can show several controls. Video narration usually needs one focus at a time.

2. **Long labels inside screenshots.** Labels drawn over the UI often hide the thing they explain. Put explanations in surrounding text.

3. **Bad coordinate boxes.** A highlight that misses the control damages trust. Create a coordinate QA montage before rendering.

4. **Caption bars that look like highlights.** If captions use the same bright stroke as feature boxes, viewers may see two highlights. Use neutral caption styling.

5. **Narration drift.** Do not guess timing. Measure per-segment audio duration and drive visual duration from it.

6. **Meta narration.** “Updated video”, “as requested”, and similar phrases make the artifact feel like an internal revision rather than final training material.

7. **Unsafe live demos.** Do not click state-changing controls for media capture unless explicitly approved.

8. **Skipping exact-file visual QA.** QA the compressed/delivered MP4, not only the high-bitrate source. QA the actual DOCX image set, not only source screenshots.

9. **Glyph/font surprises.** Checkmarks, emoji, and special bullets can render as boxes. Use simple filled dots or plain text unless verified.

10. **Overcompressed unreadable video.** If making a small delivery copy, extract frames from that copy and verify text readability.

11. **Blank tail frames.** Holding the last frame after a fade-out can create a blank ending. Hold a visible recap frame instead.

12. **Overclaiming validation.** Structural checks are not visual checks. Report both separately.

## Verification Checklist

Before final delivery, confirm:

- [ ] Source screenshots exist and are the intended UI state.
- [ ] Logo/branding asset exists or limitation is stated.
- [ ] No unapproved state-changing actions were performed.
- [ ] Storyboard maps each video scene to one feature or logical group.
- [ ] DOCX sections map to overview screenshots and/or individual crops.
- [ ] Narration contains no meta/revision/conversation-specific wording.
- [ ] Per-segment audio files were generated and measured.
- [ ] Final video has audio, duration, resolution, and known file size.
- [ ] Visual/audio drift was checked before final muxing.
- [ ] Delivery-compressed MP4, if any, was visually QA’d.
- [ ] DOCX package has `word/document.xml`, styles, relationships, and embedded images.
- [ ] Extracted DOCX text contains expected sections.
- [ ] Unwanted phrases are absent from video captions and DOCX text.
- [ ] Video frame montage from the exact delivered MP4 was inspected.
- [ ] DOCX screenshot/crop montage from exact embedded assets was inspected.
- [ ] Highlight boxes align with intended controls.
- [ ] Captions and labels do not cover highlighted controls.
- [ ] No weird glyphs, numbered artifacts, or blank ending frames remain.
- [ ] Final response includes artifact paths and compact validation evidence.
