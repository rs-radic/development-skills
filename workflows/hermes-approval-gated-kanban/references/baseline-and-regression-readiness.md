# Software-development baseline and regression readiness

Apply this policy to code, build/test-harness and runtime-dependency changes on any Kanban board, within the current role and approved scope. Project-specific commands, target branches, database prerequisites, safety restrictions and explicit owner decisions still govern. Research, documentation-only and routine administrative work do not acquire software build gates from this policy.

## Before implementation

- The planner names the project-selected source baseline, required existing regression checks, runtime/database prerequisites and the owner who will verify them. The plan reviewer checks that this is practical and in scope; planning does not authorize executing live tests or changing infrastructure.
- Before product edits, the implementation/startup owner verifies that baseline source, required binaries/dependencies and applicable database schema/procedures match. Build the baseline and run the existing checks required by the approved plan, through their actual harnesses. A healthy page or application build does not prove that a separately compiled test harness works.
- Record the exact source and test identities, commands and outcomes. Distinguish existing baseline failures, harness compilation/setup failures, skipped or unrun checks, and intentionally failing NEW feature tests. Do not label an unexecuted or broken baseline check PASS.
- Resolve a blocking baseline failure before feature edits: perform an already-authorized bounded setup/test repair, or obtain the necessary scoped correction/explicit owner disposition through the existing workflow. Never silently waive a required check or expand a protected test-edit boundary. An unrelated failure does not authorize a broad cleanup project.

## During the change

- Changes to shared code include maintenance of the affected existing test harnesses, dependency references and source-preservation expectations in the same bounded change, when that maintenance is authorized. Do not leave an old fixture unable to compile against newly introduced dependencies for the next feature to discover.
- Preserve behavioral/security assertions and legitimate baseline cases. Do not merely update hashes, replace expectations with candidate output, skip a test or substitute permissive stubs to turn it green. Independently review why a preservation expectation changed and verify that the corrected check still detects the behavior it protects.
- When maintenance is outside the approved file/test scope, report that exact conflict early for a scoped amendment; do not silently fix it or repeatedly ask for permissions already granted.

## At review and integration

- Existing SPEC/quality reviewers verify the baseline evidence, the affected-test inventory and any authorized test-maintenance delta. Keep application build, test-harness build, test outcome and live acceptance separate in reports.
- The integration/release owner verifies the final composed candidate against the project's current integration baseline, required dependencies and database state, then runs the affected regressions and other checks required at that stage. Qualified unchanged evidence may be reused under the project's rules; do not repeat the full suite at every card or drop a mandatory final check.

Use the existing owners, review topology and cleanup gate. This adds no framework, watcher, permanent role or extra card. Apply the before-code requirement to not-yet-started implementation; do not restart running work, invalidate accepted gates or rewrite frozen plans to retrofit it. For in-flight work, preserve completed evidence and apply compatible checks to remaining work; an explicit conflicting approved requirement uses the normal amendment path.
